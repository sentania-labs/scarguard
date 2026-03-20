"""Detection event processing: cooldown dedup, snapshot capture, SQLite logging."""

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from detector import Detection

logger = logging.getLogger(__name__)


class EventProcessor:
    def __init__(
        self,
        cooldown_seconds: int,
        snapshot_dir: str,
        db_path: str,
    ) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._snapshot_dir = Path(snapshot_dir)
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        # Cooldown tracker: "{camera_name}:{class_name}" → monotonic time of last event
        self._last_event: dict[str, float] = {}
        self._init_db()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def process(
        self,
        detections: list[Detection],
        camera_name: str,
        frame: np.ndarray,
    ) -> list[dict]:
        """
        Filter detections through cooldown logic and persist passing events.

        Returns a list of event dicts ready to be JSON-serialized and published.
        """
        now = time.monotonic()
        events: list[dict] = []

        for det in detections:
            key = f"{camera_name}:{det.class_name}"
            if now - self._last_event.get(key, 0.0) < self.cooldown_seconds:
                logger.debug(
                    "[%s] %s suppressed — cooldown active", camera_name, det.class_name
                )
                continue

            self._last_event[key] = now
            timestamp = datetime.now(timezone.utc)
            snapshot_path = self._save_snapshot(frame, det, camera_name, timestamp)
            self._persist(timestamp, det, camera_name, snapshot_path)

            logger.info(
                "[%s] %s detected (conf=%.2f)",
                camera_name,
                det.class_name,
                det.confidence,
            )
            events.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "class_name": det.class_name,
                    "confidence": det.confidence,
                    "camera_name": camera_name,
                    "snapshot_path": snapshot_path,
                }
            )

        return events

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _save_snapshot(
        self,
        frame: np.ndarray,
        det: Detection,
        camera_name: str,
        timestamp: datetime,
    ) -> str | None:
        try:
            annotated = frame.copy()
            x1, y1, x2, y2 = det.bbox
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 0, 255), 2)
            label = f"{det.class_name} {det.confidence:.2f}"
            cv2.putText(
                annotated,
                label,
                (x1, max(y1 - 8, 0)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 0, 255),
                2,
            )
            filename = (
                f"{camera_name}_{det.class_name}_{timestamp.strftime('%Y%m%dT%H%M%SZ')}.jpg"
            )
            path = self._snapshot_dir / filename
            cv2.imwrite(str(path), annotated)
            logger.debug("Snapshot saved: %s", path)
            return str(path)
        except Exception:
            logger.exception("Failed to save snapshot")
            return None

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS detection_events (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp     TEXT    NOT NULL,
                    class_name    TEXT    NOT NULL,
                    confidence    REAL    NOT NULL,
                    camera_name   TEXT    NOT NULL,
                    snapshot_path TEXT
                )
                """
            )

    def _persist(
        self,
        timestamp: datetime,
        det: Detection,
        camera_name: str,
        snapshot_path: str | None,
    ) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO detection_events
                        (timestamp, class_name, confidence, camera_name, snapshot_path)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        timestamp.isoformat(),
                        det.class_name,
                        det.confidence,
                        camera_name,
                        snapshot_path,
                    ),
                )
        except Exception:
            logger.exception("Failed to persist detection event to database")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)
