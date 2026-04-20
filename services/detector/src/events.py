"""Detection event processing: cooldown dedup, snapshot capture, SQLite logging."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

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
        self._cooldown_lock = threading.Lock()
        # SQLite writes are serialized through one lock and one long-lived connection.
        self._db_lock = threading.Lock()
        self._conn = self._open_connection()
        self._init_db()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def process(
        self,
        detections: list[Detection],
        camera_name: str,
        frame: np.ndarray,
        actions_by_class: dict[str, list[str]] | None = None,
        groups_by_class: dict[str, list[str]] | None = None,
    ) -> list[dict]:
        """
        Filter detections through cooldown logic and persist passing events.

        *actions_by_class* maps class_name → list of channel names to trigger.
        If absent or the class has no entry, all channels are notified (default).

        *groups_by_class* maps class_name → list of deterrent group names to
        fire.  Empty list or absent entry means "no deterrent action" —
        deterrents are explicit-opt-in per v0.13.3 and have no "all" default.

        Returns a list of event dicts ready to be JSON-serialized and published.
        """
        now = time.monotonic()
        events: list[dict] = []

        # Capture frame dimensions (h, w) for bbox normalization at export time.
        frame_h, frame_w = frame.shape[:2]
        frame_size = (frame_w, frame_h)

        for det in detections:
            key = f"{camera_name}:{det.class_name}"
            with self._cooldown_lock:
                last = self._last_event.get(key, 0.0)
                if now - last < self.cooldown_seconds:
                    logger.debug(
                        "[%s] %s suppressed — cooldown active", camera_name, det.class_name
                    )
                    continue
                self._last_event[key] = now
            timestamp = datetime.now(timezone.utc)
            snapshot_path = self._save_snapshot(frame, det, camera_name, timestamp)
            feedback_token = uuid.uuid4().hex
            # None  → action rules exist but no rule matched (suppress)
            # []    → no action rules configured (notify all channels)
            # [...]→ matched rule with specific channels
            if actions_by_class is None:
                actions_triggered: list[str] | None = []
            else:
                actions_triggered = actions_by_class.get(det.class_name)
            matched_groups: list[str] = (
                list(groups_by_class.get(det.class_name, []))
                if groups_by_class is not None else []
            )
            self._persist(
                timestamp, det, camera_name, snapshot_path,
                actions_triggered, frame_size, feedback_token,
            )

            logger.info(
                "[%s] %s detected (conf=%.2f)",
                camera_name,
                det.class_name,
                det.confidence,
            )

            if actions_triggered is None:
                logger.debug(
                    "[%s] %s no matching notification rule — notifier will suppress",
                    camera_name, det.class_name,
                )

            bbox_list = list(det.bbox) if det.bbox else None
            events.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "class_name": det.class_name,
                    "confidence": det.confidence,
                    "camera_name": camera_name,
                    "snapshot_path": snapshot_path,
                    "actions_triggered": actions_triggered,
                    "matched_groups": matched_groups,
                    "bbox": bbox_list,
                    "frame_size": list(frame_size),
                    "feedback_token": feedback_token,
                }
            )

        return events

    def log_system_event(self, event_type: str) -> None:
        """Persist an arm/disarm or other system transition to detection_events."""
        timestamp = datetime.now(timezone.utc)
        with self._db_lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO detection_events
                        (timestamp, class_name, confidence, camera_name, snapshot_path)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (timestamp.isoformat(), event_type, 1.0, "_system", None),
                )
                self._conn.commit()
            except Exception:
                logger.exception("Failed to log system event %s", event_type)
                try:
                    self._reset_connection_locked()
                except Exception:
                    logger.exception("Failed to recover SQLite connection after system event error")

    def close(self) -> None:
        """Close DB resources for a graceful shutdown."""
        with self._db_lock:
            if self._conn is None:
                return
            self._conn.close()
            self._conn = None  # type: ignore[assignment]

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
        """Save a clean (unannotated) snapshot frame to disk.

        BBox data is persisted separately in the database; the browser
        renders the overlay using stored coordinates.
        """
        try:
            # Import lazily so unit tests can run in environments without OpenCV system libs.
            import cv2

            safe_name = re.sub(r'[^\w\-]', '_', camera_name)
            filename = (
                f"{safe_name}_{det.class_name}_{timestamp.strftime('%Y%m%dT%H%M%SZ')}.jpg"
            )
            path = self._snapshot_dir / filename
            cv2.imwrite(str(path), frame)
            logger.debug("Snapshot saved: %s", path)
            return str(path)
        except Exception:
            logger.exception("Failed to save snapshot")
            return None

    def _init_db(self) -> None:
        with self._db_lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS detection_events (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp         TEXT    NOT NULL,
                    class_name        TEXT    NOT NULL,
                    confidence        REAL    NOT NULL,
                    camera_name       TEXT    NOT NULL,
                    snapshot_path     TEXT,
                    actions_triggered TEXT
                )
                """
            )
            # Migrations: add columns to existing databases
            existing = {
                row[1]
                for row in self._conn.execute(
                    "PRAGMA table_info(detection_events)"
                ).fetchall()
            }
            migrations: dict[str, str] = {
                "actions_triggered": "ALTER TABLE detection_events ADD COLUMN actions_triggered TEXT",
                "bbox": "ALTER TABLE detection_events ADD COLUMN bbox TEXT",
                "frame_size": "ALTER TABLE detection_events ADD COLUMN frame_size TEXT",
                "feedback": "ALTER TABLE detection_events ADD COLUMN feedback TEXT",
                "corrected_class": "ALTER TABLE detection_events ADD COLUMN corrected_class TEXT",
                "feedback_token": "ALTER TABLE detection_events ADD COLUMN feedback_token TEXT",
            }
            for col, ddl in migrations.items():
                if col not in existing:
                    self._conn.execute(ddl)
            self._conn.commit()

            # Visit sessions table
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS visit_sessions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    camera_name     TEXT    NOT NULL,
                    class_name      TEXT    NOT NULL,
                    start_time      TEXT    NOT NULL,
                    end_time        TEXT    NOT NULL,
                    duration_secs   REAL    NOT NULL,
                    detection_count INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_visits_camera_class "
                "ON visit_sessions(camera_name, class_name)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_visits_start "
                "ON visit_sessions(start_time)"
            )
            self._conn.commit()

            # App state table (key-value store for application state)
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

            # System metrics table
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS system_metrics (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT    NOT NULL,
                    cpu_pct         REAL,
                    gpu_pct         REAL,
                    gpu_temp        REAL,
                    ram_used_mb     INTEGER,
                    ram_total_mb    INTEGER,
                    camera_data     TEXT
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_metrics_timestamp "
                "ON system_metrics(timestamp)"
            )
            self._conn.commit()

            # Indexes on detection_events for common web UI query patterns
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_timestamp "
                "ON detection_events(timestamp)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_camera "
                "ON detection_events(camera_name)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_class "
                "ON detection_events(class_name)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_feedback "
                "ON detection_events(feedback)"
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_token "
                "ON detection_events(feedback_token)"
            )
            self._conn.commit()

    def _persist(
        self,
        timestamp: datetime,
        det: Detection,
        camera_name: str,
        snapshot_path: str | None,
        actions_triggered: list[str] | None,
        frame_size: tuple[int, int] | None = None,
        feedback_token: str | None = None,
    ) -> None:
        with self._db_lock:
            try:
                self._insert_event(
                    timestamp, det, camera_name, snapshot_path,
                    actions_triggered, frame_size, feedback_token,
                )
                self._conn.commit()
            except Exception:
                logger.exception("Failed to persist detection event to database")
                try:
                    self._reset_connection_locked()
                except Exception:
                    logger.exception("Failed to recover SQLite connection after write error")

    def _insert_event(
        self,
        timestamp: datetime,
        det: Detection,
        camera_name: str,
        snapshot_path: str | None,
        actions_triggered: list[str] | None,
        frame_size: tuple[int, int] | None = None,
        feedback_token: str | None = None,
    ) -> None:
        actions_json = json.dumps(actions_triggered) if actions_triggered is not None else None
        bbox_json = json.dumps(list(det.bbox)) if det.bbox else None
        frame_size_json = json.dumps(list(frame_size)) if frame_size else None
        self._conn.execute(
            """
            INSERT INTO detection_events
                (timestamp, class_name, confidence, camera_name, snapshot_path,
                 actions_triggered, bbox, frame_size, feedback_token)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp.isoformat(),
                det.class_name,
                det.confidence,
                camera_name,
                snapshot_path,
                actions_json,
                bbox_json,
                frame_size_json,
                feedback_token,
            ),
        )

    def _reset_connection_locked(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._conn = self._open_connection()

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn
