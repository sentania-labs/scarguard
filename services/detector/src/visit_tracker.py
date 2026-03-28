"""Visit duration tracking — groups consecutive detections into sessions."""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class _ActiveVisit:
    """In-progress visit session."""
    camera_name: str
    class_name: str
    start_time: datetime
    last_detection_time: datetime
    detection_count: int = 1


class VisitTracker:
    """Tracks consecutive detections as visit sessions.

    A visit session is defined as consecutive detections of the same class on the
    same camera with gaps shorter than ``timeout_seconds``.  When the gap exceeds
    the timeout, the session is closed and written to SQLite.
    """

    def __init__(self, db_path: str, timeout_seconds: int = 300) -> None:
        self._db_path = db_path
        self._timeout = timeout_seconds
        self._active: dict[str, _ActiveVisit] = {}  # key: "camera:class"
        self._lock = threading.Lock()
        self._db_lock = threading.Lock()
        self._conn = self._open_connection()

    def record_detection(self, camera_name: str, class_name: str, timestamp: datetime) -> None:
        """Record a detection.  Either extends an active visit or starts a new one."""
        key = f"{camera_name}:{class_name}"
        to_persist: dict | None = None
        with self._lock:
            visit = self._active.get(key)
            if visit is not None:
                elapsed = (timestamp - visit.last_detection_time).total_seconds()
                if elapsed >= self._timeout:
                    # Gap exceeded timeout — close old visit, start new one
                    duration = (visit.last_detection_time - visit.start_time).total_seconds()
                    to_persist = {
                        "camera_name": visit.camera_name,
                        "class_name": visit.class_name,
                        "start_time": visit.start_time.isoformat(),
                        "end_time": visit.last_detection_time.isoformat(),
                        "duration_secs": round(duration, 1),
                        "detection_count": visit.detection_count,
                    }
                    self._active[key] = _ActiveVisit(
                        camera_name=camera_name,
                        class_name=class_name,
                        start_time=timestamp,
                        last_detection_time=timestamp,
                    )
                else:
                    visit.last_detection_time = timestamp
                    visit.detection_count += 1
            else:
                self._active[key] = _ActiveVisit(
                    camera_name=camera_name,
                    class_name=class_name,
                    start_time=timestamp,
                    last_detection_time=timestamp,
                )
        # Persist outside the lock
        if to_persist is not None:
            self._persist(to_persist)

    def flush_expired(self) -> list[dict]:
        """Close and persist visits where the gap has exceeded the timeout.

        Returns list of closed visit dicts (for logging/publishing).
        Called periodically from the main loop.
        """
        now_utc = datetime.now(timezone.utc)
        closed: list[dict] = []
        expired_keys: list[str] = []

        with self._lock:
            for key, visit in self._active.items():
                elapsed_since_last = (now_utc - visit.last_detection_time).total_seconds()
                if elapsed_since_last >= self._timeout:
                    expired_keys.append(key)

            for key in expired_keys:
                visit = self._active.pop(key)
                duration = (visit.last_detection_time - visit.start_time).total_seconds()
                record = {
                    "camera_name": visit.camera_name,
                    "class_name": visit.class_name,
                    "start_time": visit.start_time.isoformat(),
                    "end_time": visit.last_detection_time.isoformat(),
                    "duration_secs": round(duration, 1),
                    "detection_count": visit.detection_count,
                }
                closed.append(record)

        # Persist outside the lock
        for record in closed:
            self._persist(record)
            logger.info(
                "Visit closed: %s on %s — %.0fs, %d detections",
                record["class_name"],
                record["camera_name"],
                record["duration_secs"],
                record["detection_count"],
            )

        return closed

    def flush_all(self) -> list[dict]:
        """Close all active visits (e.g. on shutdown)."""
        closed: list[dict] = []
        with self._lock:
            for key in list(self._active.keys()):
                visit = self._active.pop(key)
                duration = (visit.last_detection_time - visit.start_time).total_seconds()
                record = {
                    "camera_name": visit.camera_name,
                    "class_name": visit.class_name,
                    "start_time": visit.start_time.isoformat(),
                    "end_time": visit.last_detection_time.isoformat(),
                    "duration_secs": round(duration, 1),
                    "detection_count": visit.detection_count,
                }
                closed.append(record)
        for record in closed:
            self._persist(record)
        return closed

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _persist(self, record: dict) -> None:
        """Write a closed visit session to SQLite."""
        with self._db_lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO visit_sessions
                        (camera_name, class_name, start_time, end_time,
                         duration_secs, detection_count)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["camera_name"],
                        record["class_name"],
                        record["start_time"],
                        record["end_time"],
                        record["duration_secs"],
                        record["detection_count"],
                    ),
                )
                self._conn.commit()
            except Exception:
                logger.exception("Failed to persist visit session")
