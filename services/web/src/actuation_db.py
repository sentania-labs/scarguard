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


def get_latest_event() -> dict[str, Any] | None:
    """Return the most recent actuation event, or None if none exist."""
    conn = _connect()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM actuation_events ORDER BY id DESC LIMIT 1",
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        logger.warning("Failed to query latest actuation event", exc_info=True)
        return None
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


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile of *sorted_values* (ascending).  None if empty."""
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def get_latency_summary(last_n: int = 100) -> dict[str, Any]:
    """Return p50/p95 summary over the last *last_n* actuations.

    Aggregates three latency signals:
    - ``trigger_delay_ms`` — detection timestamp → deterrent dequeue
    - ``cloud_ack_ms`` — per-device: ON command sent → Tuya Cloud ack
    - ``total_duration_sec`` — full actuation sequence wall clock

    Returns ``{"count": N, "trigger_delay": {"p50":..., "p95":...}, ...}``
    with None values when not enough samples are available.
    """
    out: dict[str, Any] = {
        "count": 0,
        "trigger_delay_ms": {"p50": None, "p95": None},
        "cloud_ack_ms": {"p50": None, "p95": None},
        "total_duration_sec": {"p50": None, "p95": None},
    }
    conn = _connect()
    if conn is None:
        return out
    try:
        rows = conn.execute(
            "SELECT id, trigger_delay_ms, total_duration_sec FROM actuation_events "
            "ORDER BY id DESC LIMIT ?",
            (last_n,),
        ).fetchall()
        if not rows:
            return out
        out["count"] = len(rows)
        event_ids = [r["id"] for r in rows]

        trig = sorted(float(r["trigger_delay_ms"]) for r in rows if r["trigger_delay_ms"] is not None)
        dur = sorted(float(r["total_duration_sec"]) for r in rows if r["total_duration_sec"] is not None)

        placeholders = ",".join("?" * len(event_ids))
        ack_rows = conn.execute(
            f"SELECT cloud_ack_ms FROM device_actions "
            f"WHERE event_id IN ({placeholders}) AND cloud_ack_ms IS NOT NULL",
            event_ids,
        ).fetchall()
        ack = sorted(float(r["cloud_ack_ms"]) for r in ack_rows)

        out["trigger_delay_ms"] = {
            "p50": _percentile(trig, 0.50), "p95": _percentile(trig, 0.95),
        }
        out["cloud_ack_ms"] = {
            "p50": _percentile(ack, 0.50), "p95": _percentile(ack, 0.95),
        }
        out["total_duration_sec"] = {
            "p50": _percentile(dur, 0.50), "p95": _percentile(dur, 0.95),
        }
        return out
    except Exception:
        logger.warning("Failed to compute latency summary", exc_info=True)
        return out
    finally:
        conn.close()
