"""Read-only access to the deterrent service's actuation event database.

The deterrent service is the sole writer (``/data/deterrent.db``).
This module opens the file read-only for the actuation log page.
If the database does not exist yet (deterrent never ran), all queries
return empty results gracefully.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH: str = os.environ.get("DETERRENT_DB_PATH", "/data/deterrent.db")


def _connect() -> sqlite3.Connection | None:
    """Open a read-only connection, or return None if the DB doesn't exist."""
    if not Path(DB_PATH).is_file():
        return None
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def get_actuations(
    limit: int = 50,
    offset: int = 0,
    trigger_class: str | None = None,
    camera: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Return actuation events, newest first."""
    conn = _connect()
    if conn is None:
        return []
    try:
        conditions: list[str] = []
        params: list[Any] = []
        if trigger_class:
            conditions.append("trigger_class = ?")
            params.append(trigger_class)
        if camera:
            conditions.append("trigger_camera = ?")
            params.append(camera)
        if date_from:
            conditions.append("timestamp >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("timestamp <= ?")
            params.append(date_to + "T23:59:59")
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT * FROM actuation_events{where} ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.warning("Failed to query actuation events", exc_info=True)
        return []
    finally:
        conn.close()


def count_actuations(
    trigger_class: str | None = None,
    camera: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    """Count actuation events matching the given filters."""
    conn = _connect()
    if conn is None:
        return 0
    try:
        conditions: list[str] = []
        params: list[Any] = []
        if trigger_class:
            conditions.append("trigger_class = ?")
            params.append(trigger_class)
        if camera:
            conditions.append("trigger_camera = ?")
            params.append(camera)
        if date_from:
            conditions.append("timestamp >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("timestamp <= ?")
            params.append(date_to + "T23:59:59")
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        row = conn.execute(f"SELECT COUNT(*) FROM actuation_events{where}", params).fetchone()
        return row[0] if row else 0
    except Exception:
        logger.warning("Failed to count actuation events", exc_info=True)
        return 0
    finally:
        conn.close()


def get_actuation_actions(event_id: int) -> list[dict[str, Any]]:
    """Return device actions for a specific actuation event."""
    conn = _connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM device_actions WHERE event_id = ? ORDER BY id",
            (event_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.warning("Failed to query device actions for event %d", event_id, exc_info=True)
        return []
    finally:
        conn.close()
