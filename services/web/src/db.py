"""Read-only SQLite access for detection events."""

import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "/data/scarguard.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_events(limit: int = 100, offset: int = 0) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """
            SELECT id, timestamp, class_name, confidence, camera_name, snapshot_path
            FROM detection_events
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()


def get_event(event_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM detection_events WHERE id = ?", (event_id,)
        ).fetchone()


def count_events() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM detection_events").fetchone()
        return row[0] if row else 0


def get_latest_event() -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM detection_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
