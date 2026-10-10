"""ScarGuard SQLite backup sidecar.

Three SQLite databases live on the shared ``scarguard-data`` volume:

* ``scarguard.db`` - detection events, training feedback, performance metrics
* ``auth.db`` - users, sessions, API tokens, audit log
* ``deterrent.db`` - actuation events

Pre-v1.14 there was no documented backup story; a volume-level rm or an
SD-card failure on a Jetson lost everything. This sidecar runs SQLite's
online backup API (which is WAL-aware and works on a live database)
on a configurable schedule, gzips the output, and applies retention.

Operator-triggered manual backups arrive via the
``scarguard:backup:trigger`` Redis channel (admin UI action). Status
updates are published to ``scarguard:backup:status`` so the UI can
surface progress.

v1.15 verifies HMAC signatures on manual trigger requests so a
compromised container cannot trigger arbitrary backups.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import redis as redis_lib
import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/config/scarguard.yml"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
BACKUP_ROOT = DATA_DIR / "backups"

TRIGGER_CHANNEL = "scarguard:backup:trigger"
STATUS_CHANNEL = "scarguard:backup:status"

# Signing key for backup trigger requests (web → backup sidecar).
_TRIGGER_KEY: bytes | None = None
_VERIFY_TRIGGER = None
_CHANNEL_TRIGGER = None
_DERIVE_KEY = None

try:
    from event_signing import (
        CHANNEL_FIELD as _EF_CF,
    )
    from event_signing import (
        _ReplayCache,
        load_key_from_env,
    )
    from event_signing import (
        derive_channel_key as _EF_DK,
    )
    from event_signing import (
        verify_event as _EF_VE,
    )

    _TRIGGER_KEY = load_key_from_env()
    _DERIVE_KEY = _EF_DK
    _VERIFY_TRIGGER = _EF_VE
    _CHANNEL_TRIGGER = _EF_CF
    _TRIGGER_CACHE = _ReplayCache(capacity=4096, ttl_seconds=60) if _TRIGGER_KEY else None
except ImportError:
    _TRIGGER_CACHE = None


def _verify_trigger(payload: dict) -> bool:
    """Return True if *payload* is a valid signed backup trigger."""
    if _TRIGGER_KEY is None or _TRIGGER_CACHE is None:
        return True
    if _CHANNEL_TRIGGER is None or _VERIFY_TRIGGER is None:
        return True
    ch = payload.get(_CHANNEL_TRIGGER)
    if isinstance(ch, str) and ch != TRIGGER_CHANNEL:
        return False
    channel_key = _DERIVE_KEY(_TRIGGER_KEY, TRIGGER_CHANNEL) if _DERIVE_KEY else _TRIGGER_KEY
    return _VERIFY_TRIGGER(payload, channel_key, TRIGGER_CHANNEL, _TRIGGER_CACHE)  # type: ignore[arg-type]


DEFAULT_INTERVAL_HOURS = 24
DEFAULT_RETENTION_DAILY = 14
DEFAULT_RETENTION_WEEKLY = 8
DEFAULT_COMPRESS = True
# Manual (operator-triggered) snapshots kept per database, independent of
# the daily/weekly schedule retention.
MANUAL_RETENTION = 10
# Suffixes of in-progress files; never listed or restored. Cycles are
# serialized, so one found this old can only be an orphan of a cycle that
# died mid-write; prune removes it.
IN_PROGRESS_SUFFIXES = (".partial", ".tmp")
IN_PROGRESS_MAX_AGE_SECONDS = 3600
# Only files the sidecar itself named are subject to retention.
SNAPSHOT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}")

DATABASES: tuple[tuple[str, Path], ...] = (
    ("scarguard", DATA_DIR / "scarguard.db"),
    ("auth", DATA_DIR / "auth.db"),
    ("deterrent", DATA_DIR / "deterrent.db"),
)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
        stream=sys.stdout,
    )


def load_backup_config() -> dict[str, Any]:
    try:
        with CONFIG_PATH.open() as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    if not isinstance(cfg, dict):
        return {}
    raw = cfg.get("backup", {})
    return raw if isinstance(raw, dict) else {}


def _interval_seconds(cfg: dict[str, Any]) -> float:
    hours = cfg.get("interval_hours", DEFAULT_INTERVAL_HOURS)
    try:
        return max(1.0, float(hours)) * 3600.0
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_HOURS * 3600.0


def _enabled(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("enabled", True))


def _compress(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("compress", DEFAULT_COMPRESS))


def _retention(cfg: dict[str, Any]) -> tuple[int, int]:
    daily = int(cfg.get("retention_daily", DEFAULT_RETENTION_DAILY))
    weekly = int(cfg.get("retention_weekly", DEFAULT_RETENTION_WEEKLY))
    return max(1, daily), max(0, weekly)


def backup_database(
    db_name: str,
    db_path: Path,
    *,
    compress: bool,
    triggered_by: str = "schedule",
) -> Path | None:
    """Run SQLite's online backup API against *db_path* and write to disk.

    Scheduled snapshots are named ``<timestamp>.db[.gz]``; other triggers
    get a ``-<triggered_by>`` tag (``...-manual.db.gz``) so retention can
    tell them apart. The snapshot is ``quick_check``ed before it is
    published under its final name; staging files carry unique
    ``.tmp``/``.partial`` names so interrupted cycles never collide with
    or masquerade as a finished backup.

    Returns the resulting file path, or None if the source DB doesn't
    exist (e.g. fresh install, no auth.db yet)."""
    if not db_path.exists():
        logger.info("Source %s missing, skipping", db_path)
        return None

    target_dir = BACKUP_ROOT / db_name
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    suffix = ".db.gz" if compress else ".db"

    name_base = f"{timestamp}-{triggered_by}" if triggered_by != "schedule" else timestamp
    final_path = target_dir / f"{name_base}{suffix}"
    if final_path.exists():
        # Two cycles within one second (e.g. manual trigger right after a
        # restart): never overwrite the earlier snapshot.
        final_path = target_dir / f"{name_base}-{uuid.uuid4().hex[:6]}{suffix}"

    # Two-step: backup to a uniquely named tmpfile in the same dir (atomic
    # rename later), optionally gzip. SQLite's .backup API holds shared
    # locks but doesn't block writers thanks to WAL.
    fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=f"{name_base}.", suffix=".db.tmp")
    os.close(fd)
    tmp = Path(tmp_path)
    try:
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        # Only publish snapshots that pass quick_check.
        dst_check = sqlite3.connect(tmp)
        try:
            rows = dst_check.execute("PRAGMA quick_check").fetchall()
        finally:
            dst_check.close()
        if rows != [("ok",)]:
            raise ValueError(f"backup quick_check failed: {rows[:3]}")

        if compress:
            # Gzip into a sibling .partial file, then atomically rename
            # to final_path. Writing gzip directly to final_path would
            # leave a truncated .db.gz in place on interrupt/failure,
            # which restore would happily pick up as a valid snapshot.
            gz_tmp = target_dir / f"{name_base}{suffix}.{uuid.uuid4().hex[:8]}.partial"
            try:
                with open(tmp, "rb") as f_in:
                    with gzip.open(gz_tmp, "wb", compresslevel=6) as f_out:
                        shutil.copyfileobj(f_in, f_out)
                os.replace(gz_tmp, final_path)
            finally:
                if gz_tmp.exists():
                    gz_tmp.unlink()
            tmp.unlink()
        else:
            os.replace(tmp, final_path)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise

    size_kb = final_path.stat().st_size // 1024
    logger.info("Backed up %s → %s (%d KB)", db_name, final_path.name, size_kb)
    return final_path


def prune_backups(db_name: str, daily: int, weekly: int) -> int:
    """Apply retention to one database's backup directory.

    Scheduled snapshots are grouped by the ``YYYY-MM-DD`` prefix of their
    filename and the *daily* most recent calendar days are kept. Every
    snapshot of the newest day is kept (an ``interval_hours`` below 24
    gives intra-day points); each older day collapses to its newest
    snapshot, so several same-day cycles (the sidecar runs one on every
    start) can never crowd out earlier days. Beyond those days one
    snapshot per ISO week is kept for *weekly* weeks.

    Manual snapshots (``-manual`` in the name) are retained separately:
    the newest ``MANUAL_RETENTION`` are kept, so operator-triggered
    backups neither consume nor evict the daily recovery points.

    Files not named by this sidecar are left alone. In-progress
    ``.partial``/``.tmp`` files older than ``IN_PROGRESS_MAX_AGE_SECONDS``
    are orphans of a cycle that died and are removed. Returns the number
    of files deleted."""
    target_dir = BACKUP_ROOT / db_name
    if not target_dir.exists():
        return 0

    deleted = 0
    # Filenames embed the timestamp, so reverse sort is newest-first.
    files: list[Path] = []
    cutoff = datetime.now(timezone.utc).timestamp() - IN_PROGRESS_MAX_AGE_SECONDS
    for f in sorted(target_dir.glob("*.db*"), reverse=True):
        if not f.is_file():
            continue
        if f.name.endswith(IN_PROGRESS_SUFFIXES):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    deleted += 1
                    logger.warning("Removed orphaned in-progress file %s", f.name)
            except OSError:
                logger.exception("Could not remove orphaned in-progress file %s", f)
            continue
        files.append(f)

    keep: set[Path] = set()
    managed: list[Path] = []
    manual: list[Path] = []
    by_day: dict[str, list[Path]] = {}
    for f in files:
        if not SNAPSHOT_NAME.match(f.name):
            continue
        managed.append(f)
        if "-manual" in f.name:
            manual.append(f)
        else:
            by_day.setdefault(f.name[:10], []).append(f)

    keep.update(manual[:MANUAL_RETENTION])

    # by_day preserves insertion order: newest day first, newest file first.
    days = list(by_day.items())
    if days:
        keep.update(days[0][1])
    for _day, day_files in days[1:daily]:
        keep.add(day_files[0])

    if weekly > 0:
        seen_weeks: set[str] = set()
        for day, day_files in days[daily:]:
            week_key = datetime.strptime(day, "%Y-%m-%d").strftime("%G-W%V")
            if week_key in seen_weeks:
                continue
            seen_weeks.add(week_key)
            keep.add(day_files[0])
            if len(seen_weeks) >= weekly:
                break

    for f in managed:
        if f not in keep:
            try:
                f.unlink()
                deleted += 1
            except Exception:
                logger.exception("Could not delete old backup %s", f)
    if deleted:
        logger.info("Pruned %d old backups for %s", deleted, db_name)
    return deleted


# Serializes cycles across the scheduler thread and the Redis trigger
# listener. Overlapping requests wait their turn; none is dropped, but a
# second manual trigger while one is already waiting is redundant (the
# waiting one will produce an equally fresh snapshot) and is coalesced.
backup_lock = threading.Lock()
_manual_waiting = threading.Lock()


def run_backup_cycle(
    cfg: dict[str, Any],
    publisher: redis_lib.Redis | None,
    *,
    triggered_by: str = "schedule",
) -> dict[str, Any]:
    """Backup every database, apply retention, return a status summary.

    If another cycle is running this one waits for it (publishing a
    ``queued`` status so the UI keeps showing progress) and then runs.
    A manual trigger that arrives while another manual cycle is already
    waiting is coalesced into it (``skipped`` status)."""
    coalesce_slot = False
    if backup_lock.locked():
        if triggered_by == "manual":
            if not _manual_waiting.acquire(blocking=False):
                logger.info("Manual backup already queued - coalescing this trigger into it")
                summary: dict[str, Any] = {
                    "phase": "skipped",
                    "triggered_by": triggered_by,
                    "reason": "a manual backup is already queued",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                _publish_status(publisher, summary)
                return summary
            coalesce_slot = True
        logger.info("Backup cycle (%s) queued behind a running cycle", triggered_by)
        _publish_status(
            publisher,
            {
                "phase": "queued",
                "triggered_by": triggered_by,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    backup_lock.acquire()
    if coalesce_slot:
        _manual_waiting.release()
    try:
        compress = _compress(cfg)
        daily, weekly = _retention(cfg)
        started = datetime.now(timezone.utc)
        _publish_status(
            publisher,
            {
                "phase": "started",
                "triggered_by": triggered_by,
                "timestamp": started.isoformat(),
            },
        )

        results: list[dict[str, Any]] = []
        success = True
        for db_name, db_path in DATABASES:
            try:
                out = backup_database(
                    db_name, db_path, compress=compress, triggered_by=triggered_by
                )
                if out is not None:
                    results.append(
                        {
                            "db": db_name,
                            "file": out.name,
                            "size_bytes": out.stat().st_size,
                            "ok": True,
                        }
                    )
                    prune_backups(db_name, daily, weekly)
            except Exception as exc:
                logger.exception("Backup failed for %s", db_name)
                results.append({"db": db_name, "ok": False, "error": str(exc)})
                success = False

        finished = datetime.now(timezone.utc)
        summary = {
            "phase": "completed",
            "triggered_by": triggered_by,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "success": success,
            "results": results,
        }
        _publish_status(publisher, summary)
        return summary
    finally:
        backup_lock.release()


def _publish_status(client: redis_lib.Redis | None, payload: dict[str, Any]) -> None:
    if client is None:
        return
    try:
        client.publish(STATUS_CHANNEL, json.dumps(payload))
    except Exception:
        logger.exception("Failed to publish backup status")


def trigger_listener(
    cfg_holder: dict[str, dict[str, Any]],
    redis_cfg: dict[str, Any],
    shutdown_event: threading.Event,
) -> None:
    """Listen for manual-trigger requests on Redis and run a backup cycle.

    Run in a daemon thread; survives Redis disconnects with backoff."""
    delay = 5
    while not shutdown_event.is_set():
        client: redis_lib.Redis | None = None
        pubsub: Any = None
        try:
            client = _make_redis(redis_cfg)
            pubsub = client.pubsub()
            pubsub.subscribe(TRIGGER_CHANNEL)
            logger.info("Subscribed to %s", TRIGGER_CHANNEL)
            delay = 5
            for message in pubsub.listen():
                if shutdown_event.is_set():
                    break
                if message["type"] != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
                    if not isinstance(payload, dict):
                        logger.warning("Malformed backup trigger (not a dict)")
                        continue
                except json.JSONDecodeError:
                    logger.warning("Malformed backup trigger: invalid JSON")
                    continue

                if not _verify_trigger(payload):
                    logger.warning("Rejected unsigned/malformed backup trigger")
                    continue

                logger.info("Manual backup triggered via Redis")
                run_backup_cycle(
                    cfg_holder["cfg"],
                    client,
                    triggered_by="manual",
                )
        except redis_lib.RedisError:
            if shutdown_event.is_set():
                break
            logger.exception("Redis error in trigger listener - retrying in %ds", delay)
            shutdown_event.wait(delay)
            delay = min(delay * 2, 60)
        finally:
            if pubsub is not None:
                try:
                    pubsub.unsubscribe()
                    pubsub.close()
                except Exception:
                    pass
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass


def _make_redis(redis_cfg: dict[str, Any]) -> redis_lib.Redis:
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))
    password = os.environ.get("REDIS_PASSWORD", "") or None
    return redis_lib.Redis(
        host=host,
        port=port,
        password=password,
        decode_responses=True,
    )


def main() -> None:
    setup_logging()
    logger.info("ScarGuard backup sidecar starting")
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)

    backup_cfg = load_backup_config()
    if not _enabled(backup_cfg):
        logger.info("Backup disabled in config - sidecar will idle")

    cfg_holder = {"cfg": backup_cfg}
    shutdown_event = threading.Event()

    def _shutdown(sig: int, _frame: object) -> None:
        logger.info("Received signal %s - shutting down", sig)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Read top-level config to find Redis endpoint.
    try:
        with CONFIG_PATH.open() as f:
            full_cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        full_cfg = {}
    redis_cfg = full_cfg.get("redis", {}) if isinstance(full_cfg, dict) else {}

    # Manual-trigger listener
    trigger_thread = threading.Thread(
        target=trigger_listener,
        name="backup-trigger",
        daemon=True,
        args=(cfg_holder, redis_cfg, shutdown_event),
    )
    trigger_thread.start()

    # Periodic loop
    publisher: redis_lib.Redis | None = None
    while not shutdown_event.is_set():
        # Refresh config each cycle so changes in scarguard.yml take effect
        # without restarting the sidecar.
        backup_cfg = load_backup_config()
        cfg_holder["cfg"] = backup_cfg

        if _enabled(backup_cfg):
            if publisher is None:
                try:
                    publisher = _make_redis(redis_cfg)
                except Exception:
                    publisher = None
            try:
                run_backup_cycle(backup_cfg, publisher, triggered_by="schedule")
            except Exception:
                logger.exception("Backup cycle raised - continuing")

        # Wait for next interval, exit early on shutdown.
        if shutdown_event.wait(_interval_seconds(backup_cfg)):
            break

    logger.info("Backup sidecar stopped cleanly")


if __name__ == "__main__":
    main()
