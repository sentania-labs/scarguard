"""SQLite persistence for actuation events.

The deterrent service is the sole writer.  The web service reads this DB
read-only for the actuation log page.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading

from actuation_models import ActuationEvent

logger = logging.getLogger(__name__)

DB_PATH: str = os.environ.get("DETERRENT_DB_PATH", "/data/deterrent.db")

_lock = threading.Lock()
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Return a thread-local connection (created on first use per thread)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def init_db() -> None:
    """Create tables and indexes if they don't exist."""
    with _lock:
        conn = _get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS actuation_events (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp           TEXT    NOT NULL,
                trigger_class       TEXT    NOT NULL,
                trigger_camera      TEXT    NOT NULL,
                trigger_confidence  REAL    NOT NULL,
                pre_delay_sec       REAL    NOT NULL,
                total_duration_sec  REAL    NOT NULL,
                device_count        INTEGER NOT NULL,
                success_count       INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_actuation_ts
                ON actuation_events(timestamp);

            CREATE TABLE IF NOT EXISTS device_actions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id        INTEGER NOT NULL REFERENCES actuation_events(id),
                device_name     TEXT    NOT NULL,
                device_id       TEXT    NOT NULL,
                device_type     TEXT    NOT NULL,
                duration_sec    REAL    NOT NULL,
                delay_before_sec REAL   NOT NULL,
                success         INTEGER NOT NULL DEFAULT 0,
                error           TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_actions_event
                ON device_actions(event_id);
        """)
        conn.commit()
        logger.info("Actuation database initialised at %s", DB_PATH)


def insert_event(event: ActuationEvent) -> int:
    """Persist an actuation event and its device actions.  Returns the row ID."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            """INSERT INTO actuation_events
               (timestamp, trigger_class, trigger_camera, trigger_confidence,
                pre_delay_sec, total_duration_sec, device_count, success_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.timestamp,
                event.trigger_class,
                event.trigger_camera,
                event.trigger_confidence,
                event.pre_delay_sec,
                event.total_duration_sec,
                len(event.actions),
                sum(1 for a in event.actions if a.success),
            ),
        )
        event_id = cur.lastrowid
        if event_id is None:
            raise RuntimeError("Failed to get lastrowid after INSERT")
        for action in event.actions:
            conn.execute(
                """INSERT INTO device_actions
                   (event_id, device_name, device_id, device_type,
                    duration_sec, delay_before_sec, success, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    action.device_name,
                    action.device_id,
                    action.device_type,
                    action.duration_sec,
                    action.delay_before_sec,
                    int(action.success),
                    action.error,
                ),
            )
        conn.commit()
        logger.debug("Actuation event %d persisted (%d actions)", event_id, len(event.actions))
        return event_id
