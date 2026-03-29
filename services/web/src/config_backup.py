"""Config backup manager — auto-backup and restore for scarguard.yml."""

from __future__ import annotations

import difflib
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/config/backups"))
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/config/scarguard.yml"))

# Maximum number of backups to keep
MAX_BACKUPS = 50


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
        self._thread = threading.Thread(
            target=self._watch_loop, name="config-backup", daemon=True
        )
        self._thread.start()
        logger.info("ConfigBackupManager started (debounce=%ds)", self._debounce)

    def stop(self) -> None:
        """Signal the watcher thread to stop."""
        self._stop.set()

    def _watch_loop(self) -> None:
        """Poll config mtime, backup when changed (debounced)."""
        # Initialize mtime
        try:
            self._last_mtime_ns = CONFIG_PATH.stat().st_mtime_ns
        except FileNotFoundError:
            pass

        while not self._stop.wait(30):  # check every 30s
            try:
                current_mtime_ns = CONFIG_PATH.stat().st_mtime_ns
            except FileNotFoundError:
                continue

            if (
                self._last_mtime_ns is not None
                and current_mtime_ns != self._last_mtime_ns
            ):
                # Config changed — wait for debounce period to catch rapid edits
                self._stop.wait(self._debounce)
                if self._stop.is_set():
                    break
                # Re-read mtime (may have changed again during debounce)
                try:
                    current_mtime_ns = CONFIG_PATH.stat().st_mtime_ns
                except FileNotFoundError:
                    continue
                self._create_backup("auto")

            self._last_mtime_ns = current_mtime_ns

    def create_backup(self, reason: str = "manual") -> str | None:
        """Create a backup. Returns the backup filename or None on failure."""
        return self._create_backup(reason)

    def _create_backup(self, reason: str) -> str | None:
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            filename = f"scarguard_{ts}_{reason}.yml"
            dest = BACKUP_DIR / filename
            shutil.copy2(str(CONFIG_PATH), str(dest))
            logger.info("Config backup created: %s", filename)
            self._prune()
            return filename
        except Exception:
            logger.exception("Failed to create config backup")
            return None

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

    def get_diff(self, backup_name: str) -> str | None:
        """Return a unified diff between a backup and the current config."""
        backup_path = BACKUP_DIR / backup_name
        # Validate path stays in BACKUP_DIR
        if not backup_path.resolve().is_relative_to(BACKUP_DIR.resolve()):
            return None
        if not backup_path.exists():
            return None
        try:
            backup_lines = backup_path.read_text().splitlines(keepends=True)
            current_lines = CONFIG_PATH.read_text().splitlines(keepends=True)
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

    def restore(self, backup_name: str) -> bool:
        """Restore a backup to the active config. Creates a pre-restore backup first."""
        backup_path = BACKUP_DIR / backup_name
        if not backup_path.resolve().is_relative_to(BACKUP_DIR.resolve()):
            return False
        if not backup_path.exists():
            return False
        try:
            # Create pre-restore backup
            self._create_backup("pre-restore")
            shutil.copy2(str(backup_path), str(CONFIG_PATH))
            logger.info("Config restored from backup: %s", backup_name)
            return True
        except Exception:
            logger.exception("Failed to restore config backup")
            return False

    def _prune(self) -> None:
        """Keep only the newest MAX_BACKUPS files."""
        try:
            files = sorted(BACKUP_DIR.glob("scarguard_*.yml"), reverse=True)
            for f in files[MAX_BACKUPS:]:
                f.unlink()
                logger.debug("Pruned old backup: %s", f.name)
        except Exception:
            logger.exception("Failed to prune backups")
