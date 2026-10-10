"""Config backup manager - auto-backup and restore for scarguard.yml."""

from __future__ import annotations

import difflib
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import config_store
import secret_box
import yaml
from config_model import validate_full_config

logger = logging.getLogger(__name__)

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/config/backups"))

# Maximum number of backups to keep
MAX_BACKUPS = 50

# Same ceiling as the raw-YAML editor: no legitimate scarguard.yml is close.
MAX_RESTORE_BYTES = 1_000_000


class RestoreError(Exception):
    """A restore was refused or failed; the live config was not replaced.

    ``message`` is safe to show the operator: it names fields and rules,
    never config values.
    """

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _publish_exclusive(tmp_path: str, dest: Path, data: bytes) -> None:
    """Give the fsynced *tmp_path* the name *dest*, never replacing a file.

    Hard-linking is atomic and exclusive. Filesystems without hard links
    (some CIFS/FUSE mounts) fall back to an exclusive create plus write.
    Raises FileExistsError if *dest* is taken.
    """
    try:
        os.link(tmp_path, dest)
        return
    except FileExistsError:
        raise
    except OSError:
        pass
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        os.unlink(dest)
        raise


def _config_path() -> Path:
    # Read at call time so the backup manager and config_store always agree
    # on which file is live.
    return config_store.CONFIG_PATH


class ConfigBackupManager:
    """Watches config file for changes and creates timestamped backups."""

    def __init__(self, debounce_seconds: int = 180) -> None:
        self._debounce = debounce_seconds
        self._last_mtime_ns: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        """Start the background watcher thread."""
        self._thread = threading.Thread(target=self._watch_loop, name="config-backup", daemon=True)
        self._thread.start()
        logger.info("ConfigBackupManager started (debounce=%ds)", self._debounce)

    def stop(self) -> None:
        """Signal the watcher thread to stop."""
        self._stop.set()

    def _watch_loop(self) -> None:
        """Poll config mtime, backup when changed (debounced)."""
        # Initialize mtime
        try:
            self._last_mtime_ns = _config_path().stat().st_mtime_ns
        except FileNotFoundError:
            pass

        while not self._stop.wait(30):  # check every 30s
            try:
                current_mtime_ns = _config_path().stat().st_mtime_ns
            except FileNotFoundError:
                continue

            if self._last_mtime_ns is not None and current_mtime_ns != self._last_mtime_ns:
                # Config changed - wait for debounce period to catch rapid edits
                self._stop.wait(self._debounce)
                if self._stop.is_set():
                    break
                # Re-read mtime (may have changed again during debounce)
                try:
                    current_mtime_ns = _config_path().stat().st_mtime_ns
                except FileNotFoundError:
                    continue
                self._create_backup("auto")

            self._last_mtime_ns = current_mtime_ns

    def create_backup(self, reason: str = "manual") -> str | None:
        """Create a backup. Returns the backup filename or None on failure."""
        return self._create_backup(reason)

    def _create_backup(self, reason: str) -> str | None:
        """Copy the live config to a new, uniquely named backup file.

        The copy is written to a temporary file, flushed, then hard-linked
        to its final name, which fails rather than overwrites if the name is
        taken - two backups in the same second (a manual backup next to an
        automatic one, or a pre-restore backup) never replace each other,
        and a crash mid-copy never leaves a truncated backup behind.
        """
        tmp_path: str | None = None
        try:
            data = _config_path().read_bytes()
            fd, tmp_path = tempfile.mkstemp(dir=str(BACKUP_DIR), prefix=".backup-", suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            for attempt in range(100):
                suffix = f"-{attempt}" if attempt else ""
                filename = f"scarguard_{ts}_{reason}{suffix}.yml"
                try:
                    _publish_exclusive(tmp_path, BACKUP_DIR / filename, data)
                except FileExistsError:
                    continue
                logger.info("Config backup created: %s", filename)
                self._prune(keep=filename)
                return filename
            logger.error("Failed to create config backup: no free backup name")
            return None
        except Exception:
            logger.exception("Failed to create config backup")
            return None
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def list_backups(self) -> list[dict[str, object]]:
        """Return list of backup info dicts, newest first."""
        backups: list[dict[str, object]] = []
        try:
            for f in sorted(BACKUP_DIR.glob("scarguard_*.yml"), reverse=True):
                stat = f.stat()
                backups.append(
                    {
                        "name": f.name,
                        "size_bytes": stat.st_size,
                        "created": datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                    }
                )
        except Exception:
            logger.exception("Failed to list backups")
        return backups

    def get_diff(
        self,
        backup_name: str,
        *,
        transform: Callable[[str], str] | None = None,
    ) -> str | None:
        """Return a unified diff between a backup and the current config.

        If *transform* is provided, it is applied to both the backup and
        the current YAML text *before* diffing.  This is used by the
        viewer-role code path to redact sensitive fields via
        ``config_redact.redact_yaml`` so secret-line changes don't leak
        through the diff output.
        """
        backup_path = BACKUP_DIR / backup_name
        # Validate path stays in BACKUP_DIR
        if not backup_path.resolve().is_relative_to(BACKUP_DIR.resolve()):
            return None
        if not backup_path.exists():
            return None
        try:
            backup_text = backup_path.read_text()
            current_text = _config_path().read_text()
            if transform is not None:
                backup_text = transform(backup_text)
                current_text = transform(current_text)
            backup_lines = backup_text.splitlines(keepends=True)
            current_lines = current_text.splitlines(keepends=True)
            diff = difflib.unified_diff(
                backup_lines,
                current_lines,
                fromfile=f"backup/{backup_name}",
                tofile="current/scarguard.yml",
            )
            return "".join(diff) or "(no differences)"
        except Exception:
            logger.exception("Failed to generate diff")
            return None

    def restore(self, backup_name: str) -> str | None:
        """Replace the live config with a backup, or refuse and change nothing.

        The backup is parsed and checked against the full config schema,
        its secrets must be storable encrypted under the existing key, and
        the current config is saved as a ``pre-restore`` backup before the
        new document is written atomically by ``config_store.save``.

        Returns the name of the pre-restore backup (None if there was no
        live config to keep). Raises :class:`RestoreError` otherwise.
        """
        backup_path = BACKUP_DIR / backup_name
        if not backup_path.resolve().is_relative_to(BACKUP_DIR.resolve()):
            raise RestoreError("Backup not found", 404)
        if not backup_path.is_file():
            raise RestoreError("Backup not found", 404)

        try:
            raw = backup_path.read_bytes()
        except OSError:
            logger.exception("Failed to read config backup %s", backup_name)
            raise RestoreError("Backup could not be read", 500)
        if len(raw) > MAX_RESTORE_BYTES:
            raise RestoreError("Backup is larger than the 1 MB config limit")
        try:
            cfg = yaml.safe_load(raw)
        except yaml.YAMLError:
            raise RestoreError("Backup is not valid YAML; the current config was kept")
        errors = validate_full_config(cfg)
        if errors:
            logger.warning(
                "Refused restore of %s: %d validation error(s): %s",
                backup_name,
                len(errors),
                "; ".join(errors),
            )
            raise RestoreError(
                "Backup failed config validation; the current config was kept: "
                + "; ".join(errors[:5])
                + ("; ..." if len(errors) > 5 else ""),
            )
        if not isinstance(cfg, dict):  # already refused above; narrows the type
            raise RestoreError("Backup is not a YAML mapping")

        try:
            config_store.require_encrypted_secrets(cfg, secret_box.try_load_key())
        except config_store.SecretEncryptionError as exc:
            raise RestoreError(
                f"Backup secrets cannot be restored safely ({exc}); the current config was kept",
            )

        pre_restore: str | None = None
        if _config_path().exists():
            pre_restore = self._create_backup("pre-restore")
            if pre_restore is None:
                raise RestoreError(
                    "Could not save the current config as a pre-restore backup; "
                    "nothing was changed",
                    500,
                )
        try:
            config_store.save(cfg, require_encryption=True)
        except config_store.SecretEncryptionError as exc:
            raise RestoreError(
                f"Backup secrets cannot be restored safely ({exc}); the current config was kept",
            )
        except Exception:
            logger.exception("Failed to write restored config from %s", backup_name)
            raise RestoreError(
                "Writing the restored config failed; the current config was kept"
                + (f" (pre-restore backup {pre_restore})" if pre_restore else ""),
                500,
            )
        logger.info(
            "Config restored from backup %s (pre-restore backup: %s)",
            backup_name,
            pre_restore,
        )
        return pre_restore

    def _prune(self, keep: str | None = None) -> None:
        """Keep only the newest MAX_BACKUPS files (never the one just made)."""
        try:
            files = sorted(BACKUP_DIR.glob("scarguard_*.yml"), reverse=True)
            for f in files[MAX_BACKUPS:]:
                if f.name == keep:
                    continue
                f.unlink()
                logger.debug("Pruned old backup: %s", f.name)
        except Exception:
            logger.exception("Failed to prune backups")
