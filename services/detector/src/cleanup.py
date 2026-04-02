"""Data retention — periodically prune old snapshots, events, visits, and metrics."""

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DAILY_SECONDS = 24 * 3600


class RetentionCleaner:
    """Deletes data older than *retention_days* on a daily cycle.

    Prunes: snapshot files on disk, detection_events (unlabeled only),
    visit_sessions, and system_metrics rows.  Labeled events (training data)
    and _system events (arm/disarm audit trail) are never pruned.

    Runs once at startup then every 24 hours as a daemon thread.  Setting
    *retention_days* to 0 or a negative value disables all cleanup.
    """

    def __init__(
        self,
        snapshot_dir: str,
        db_path: str,
        retention_days: int = 90,
    ) -> None:
        self._snapshot_dir = Path(snapshot_dir)
        self._db_path = db_path
        self.retention_days = retention_days
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="retention-cleanup", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        logger.info("Retention cleaner started — retention=%d days", self.retention_days)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        self._run()
        while not self._stop.wait(timeout=_DAILY_SECONDS):
            self._run()

    def _run(self) -> None:
        if self.retention_days <= 0:
            logger.debug("Data retention disabled (retention_days=%d)", self.retention_days)
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        cutoff_iso = cutoff.isoformat()
        logger.info("Retention cleanup — cutoff=%s", cutoff.date().isoformat())

        self._prune_snapshots(cutoff)
        self._prune_db(cutoff_iso)

    def _prune_snapshots(self, cutoff: datetime) -> None:
        """Delete snapshot files older than cutoff and NULL their DB paths."""
        deleted: list[str] = []
        try:
            for f in self._snapshot_dir.iterdir():
                if not f.is_file():
                    continue
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    try:
                        f.unlink()
                        deleted.append(str(f))
                        logger.debug("Deleted snapshot: %s", f.name)
                    except OSError:
                        logger.warning("Could not delete snapshot %s", f, exc_info=True)
        except OSError:
            logger.warning("Could not iterate snapshot directory %s", self._snapshot_dir, exc_info=True)
            return

        if deleted:
            self._clear_db_paths(deleted)
            logger.info("Snapshots pruned: %d file(s)", len(deleted))
        else:
            logger.info("Snapshots: nothing to prune")

    def _clear_db_paths(self, paths: list[str]) -> None:
        """Set snapshot_path = NULL for any DB rows whose file was deleted."""
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                placeholders = ",".join("?" * len(paths))
                conn.execute(
                    f"UPDATE detection_events SET snapshot_path = NULL"
                    f" WHERE snapshot_path IN ({placeholders})",
                    paths,
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.warning("Failed to clear snapshot paths in database", exc_info=True)

    def _prune_db(self, cutoff_iso: str) -> None:
        """Prune old events, visits, and metrics from the database."""
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                conn.execute("PRAGMA journal_mode=WAL")

                # Events: only unlabeled, non-system events
                cur = conn.execute(
                    "DELETE FROM detection_events"
                    " WHERE timestamp < ?"
                    " AND feedback IS NULL"
                    " AND camera_name != '_system'",
                    (cutoff_iso,),
                )
                events_deleted = cur.rowcount

                # Visit sessions
                cur = conn.execute(
                    "DELETE FROM visit_sessions WHERE start_time < ?",
                    (cutoff_iso,),
                )
                visits_deleted = cur.rowcount

                # System metrics
                cur = conn.execute(
                    "DELETE FROM system_metrics WHERE timestamp < ?",
                    (cutoff_iso,),
                )
                metrics_deleted = cur.rowcount

                conn.commit()

                if events_deleted or visits_deleted or metrics_deleted:
                    logger.info(
                        "DB pruned: %d event(s), %d visit(s), %d metric sample(s)",
                        events_deleted,
                        visits_deleted,
                        metrics_deleted,
                    )
                else:
                    logger.info("DB: nothing to prune")
            finally:
                conn.close()
        except Exception:
            logger.warning("Failed to prune database records", exc_info=True)
