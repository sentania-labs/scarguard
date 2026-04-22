"""ScarGuard SQLite backup sidecar.

Three SQLite databases live on the shared ``scarguard-data`` volume:

* ``scarguard.db`` — detection events, training feedback, performance metrics
* ``auth.db`` — users, sessions, API tokens, audit log
* ``deterrent.db`` — actuation events

Pre-v1.14 there was no documented backup story; a volume-level rm or an
SD-card failure on a Jetson lost everything. This sidecar runs SQLite's
online backup API (which is WAL-aware and works on a live database)
on a configurable schedule, gzips the output, and applies retention.

Operator-triggered manual backups arrive via the
``scarguard:backup:trigger`` Redis channel (admin UI action). Status
updates are published to ``scarguard:backup:status`` so the UI can
surface progress.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import signal
import sqlite3
import sys
import tempfile
import threading
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

DEFAULT_INTERVAL_HOURS = 24
DEFAULT_RETENTION_DAILY = 14
DEFAULT_RETENTION_WEEKLY = 8
DEFAULT_COMPRESS = True

DATABASES: tuple[tuple[str, Path], ...] = (
    ("scarguard", DATA_DIR / "scarguard.db"),
    ("auth", DATA_DIR / "auth.db"),
    ("deterrent", DATA_DIR / "deterrent.db"),
)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
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
) -> Path | None:
    """Run SQLite's online backup API against *db_path* and write to disk.

    Returns the resulting file path, or None if the source DB doesn't
    exist (e.g. fresh install, no auth.db yet)."""
    if not db_path.exists():
        logger.info("Source %s missing, skipping", db_path)
        return None

    target_dir = BACKUP_ROOT / db_name
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    suffix = ".db.gz" if compress else ".db"
    final_path = target_dir / f"{timestamp}{suffix}"

    # Two-step: backup to a tmpfile in the same dir (atomic rename later),
    # optionally gzip. SQLite's .backup API holds shared locks but doesn't
    # block writers thanks to WAL.
    fd, tmp_path = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
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

        if compress:
            with open(tmp, "rb") as f_in:
                with gzip.open(final_path, "wb", compresslevel=6) as f_out:
                    shutil.copyfileobj(f_in, f_out)
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
    """Apply retention. Keep the *daily* most recent files plus *weekly*
    additional files spaced ~7 days apart. Returns count of files deleted.

    Sorting by filename works because filenames embed an ISO 8601-ish
    timestamp."""
    target_dir = BACKUP_ROOT / db_name
    if not target_dir.exists():
        return 0

    files = sorted(target_dir.glob("*.db*"), reverse=True)  # newest first
    keep: set[Path] = set()

    # Keep the N most recent as dailies.
    for f in files[:daily]:
        keep.add(f)

    # From the rest, sample one per week-ish based on filename date.
    if weekly > 0 and len(files) > daily:
        seen_weeks: set[str] = set()
        for f in files[daily:]:
            try:
                # Filename starts with YYYY-MM-DD; ISO week-ish bucket.
                date_part = f.name[:10]  # "2026-04-22"
                dt = datetime.strptime(date_part, "%Y-%m-%d")
                week_key = dt.strftime("%G-W%V")
            except (ValueError, IndexError):
                continue
            if week_key in seen_weeks:
                continue
            seen_weeks.add(week_key)
            keep.add(f)
            if len(seen_weeks) >= weekly:
                break

    deleted = 0
    for f in files:
        if f not in keep:
            try:
                f.unlink()
                deleted += 1
            except Exception:
                logger.exception("Could not delete old backup %s", f)
    if deleted:
        logger.info("Pruned %d old backups for %s", deleted, db_name)
    return deleted


def run_backup_cycle(
    cfg: dict[str, Any],
    publisher: redis_lib.Redis | None,
    *,
    triggered_by: str = "schedule",
) -> dict[str, Any]:
    """Backup every database, apply retention, return a status summary."""
    compress = _compress(cfg)
    daily, weekly = _retention(cfg)
    started = datetime.now(timezone.utc)
    _publish_status(publisher, {
        "phase": "started",
        "triggered_by": triggered_by,
        "timestamp": started.isoformat(),
    })

    results: list[dict[str, Any]] = []
    success = True
    for db_name, db_path in DATABASES:
        try:
            out = backup_database(db_name, db_path, compress=compress)
            if out is not None:
                results.append({
                    "db": db_name,
                    "file": out.name,
                    "size_bytes": out.stat().st_size,
                    "ok": True,
                })
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
                logger.info("Manual backup triggered via Redis")
                run_backup_cycle(
                    cfg_holder["cfg"], client, triggered_by="manual",
                )
        except redis_lib.RedisError:
            if shutdown_event.is_set():
                break
            logger.exception("Redis error in trigger listener — retrying in %ds", delay)
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
        host=host, port=port, password=password, decode_responses=True,
    )


def main() -> None:
    setup_logging()
    logger.info("ScarGuard backup sidecar starting")
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)

    backup_cfg = load_backup_config()
    if not _enabled(backup_cfg):
        logger.info("Backup disabled in config — sidecar will idle")

    cfg_holder = {"cfg": backup_cfg}
    shutdown_event = threading.Event()

    def _shutdown(sig: int, _frame: object) -> None:
        logger.info("Received signal %s — shutting down", sig)
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
                logger.exception("Backup cycle raised — continuing")

        # Wait for next interval, exit early on shutdown.
        if shutdown_event.wait(_interval_seconds(backup_cfg)):
            break

    logger.info("Backup sidecar stopped cleanly")


if __name__ == "__main__":
    main()
