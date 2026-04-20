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
    """Create tables and indexes if they don't exist.  v0.13.3 adds three
    latency columns + group_name — all additive, migrated with ALTER on
    pre-existing databases."""
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
        # v0.13.3 additive columns — safe to re-run on existing DBs.
        _add_column_if_missing(conn, "actuation_events", "group_name", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "actuation_events", "trigger_delay_ms", "REAL")
        _add_column_if_missing(conn, "actuation_events", "queue_depth", "INTEGER")
        _add_column_if_missing(conn, "device_actions", "cloud_ack_ms", "REAL")
        conn.commit()
        logger.info("Actuation database initialised at %s", DB_PATH)


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, type_clause: str,
) -> None:
    """Add *column* of *type_clause* to *table* if it doesn't already exist."""
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in cols:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_clause}")
    logger.info("Added column %s.%s", table, column)


def insert_event(event: ActuationEvent) -> int:
    """Persist an actuation event and its device actions.  Returns the row ID."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            """INSERT INTO actuation_events
               (timestamp, trigger_class, trigger_camera, trigger_confidence,
                pre_delay_sec, total_duration_sec, device_count, success_count,
                group_name, trigger_delay_ms, queue_depth)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.timestamp,
                event.trigger_class,
                event.trigger_camera,
                event.trigger_confidence,
                event.pre_delay_sec,
                event.total_duration_sec,
                len(event.actions),
                sum(1 for a in event.actions if a.success),
                event.group_name,
                event.trigger_delay_ms,
                event.queue_depth,
            ),
        )
        event_id = cur.lastrowid
        if event_id is None:
            raise RuntimeError("Failed to get lastrowid after INSERT")
        for action in event.actions:
            conn.execute(
                """INSERT INTO device_actions
                   (event_id, device_name, device_id, device_type,
                    duration_sec, delay_before_sec, success, error, cloud_ack_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    action.device_name,
                    action.device_id,
                    action.device_type,
                    action.duration_sec,
                    action.delay_before_sec,
                    int(action.success),
                    action.error,
                    action.cloud_ack_ms,
                ),
            )
        conn.commit()
        logger.debug("Actuation event %d persisted (%d actions)", event_id, len(event.actions))
        return event_id
