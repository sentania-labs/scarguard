"""Snapshot retention — periodically prune old snapshots and clear DB references."""

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DAILY_SECONDS = 24 * 3600


class SnapshotCleaner:
    """Deletes snapshot files older than *retention_days* and NULLs their DB rows.

    Runs once at startup then every 24 hours as a daemon thread.  Setting
    *retention_days* to 0 or a negative value disables cleanup entirely.
    """

    def __init__(
        self,
        snapshot_dir: str,
        db_path: str,
        retention_days: int = 30,
    ) -> None:
        self._snapshot_dir = Path(snapshot_dir)
        self._db_path = db_path
        self.retention_days = retention_days
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="snapshot-cleanup", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        logger.info("Snapshot cleaner started — retention=%d days", self.retention_days)

    def stop(self) -> None:
        # Signal the loop to exit; no join() because the thread is daemon=True
        # and main() exits shortly after calling stop().
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
            logger.debug("Snapshot retention disabled (retention_days=%d)", self.retention_days)
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        logger.info("Snapshot cleanup — cutoff=%s", cutoff.date().isoformat())

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
            logger.info("Snapshot cleanup: removed %d file(s)", len(deleted))
        else:
            logger.info("Snapshot cleanup: nothing to remove")

    def _clear_db_paths(self, paths: list[str]) -> None:
        """Set snapshot_path = NULL for any DB rows whose file was deleted.

        Opens its own short-lived connection.  WAL mode allows concurrent readers
        and one writer; the timeout=30 handles the rare case where EventProcessor
        is mid-write when cleanup runs (once daily).
        """
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
