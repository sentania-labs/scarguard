"""SQLite access for detection events.

The detector service is the primary writer (INSERTs).  The web service writes
only to the ``feedback`` and ``corrected_class`` columns via UPDATE.
"""

import os
import sqlite3
from datetime import date, datetime, timedelta, timezone

DB_PATH = os.environ.get("DB_PATH", "/data/scarguard.db")


def _date_to_exclusive(date_str: str) -> str:
    """Shift a YYYY-MM-DD end-date forward one day for an exclusive upper bound.

    Stored timestamps include a time component (e.g. 2026-03-25T14:10:00+00:00),
    so comparing ``timestamp < '2026-03-25'`` excludes all events on that day.
    Advancing to the next day makes the filter inclusive of the selected end date.
    """
    return (date.fromisoformat(date_str) + timedelta(days=1)).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def get_events(
    limit: int = 100,
    offset: int = 0,
    camera: str | None = None,
    class_name: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    feedback: str | None = None,
    include_system: bool = False,
) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list[object] = []
    if not include_system:
        where.append("camera_name != '_system'")
    if camera:
        where.append("camera_name = ?")
        params.append(camera)
    if class_name:
        where.append("class_name = ?")
        params.append(class_name)
    if date_from:
        where.append("timestamp >= ?")
        params.append(date_from)
    if date_to:
        where.append("timestamp < ?")
        params.append(_date_to_exclusive(date_to))
    if feedback == "unreviewed":
        where.append("feedback IS NULL")
    elif feedback == "reviewed":
        where.append("feedback IS NOT NULL")

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params += [limit, offset]
    with _connect() as conn:
        return conn.execute(
            f"""
            SELECT id, timestamp, class_name, confidence, camera_name,
                   snapshot_path, actions_triggered, bbox, frame_size,
                   feedback, corrected_class
            FROM detection_events
            {clause}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()


def get_event(event_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM detection_events WHERE id = ?", (event_id,)
        ).fetchone()


def count_events(
    camera: str | None = None,
    class_name: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    feedback: str | None = None,
    include_system: bool = False,
) -> int:
    where: list[str] = []
    params: list[object] = []
    if not include_system:
        where.append("camera_name != '_system'")
    if camera:
        where.append("camera_name = ?")
        params.append(camera)
    if class_name:
        where.append("class_name = ?")
        params.append(class_name)
    if date_from:
        where.append("timestamp >= ?")
        params.append(date_from)
    if date_to:
        where.append("timestamp < ?")
        params.append(_date_to_exclusive(date_to))
    if feedback == "unreviewed":
        where.append("feedback IS NULL")
    elif feedback == "reviewed":
        where.append("feedback IS NOT NULL")

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM detection_events {clause}", params
        ).fetchone()
        return row[0] if row else 0


def get_latest_event() -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM detection_events WHERE camera_name != '_system' ORDER BY id DESC LIMIT 1"
        ).fetchone()


def get_latest_snapshots_by_camera() -> dict[str, str]:
    """Return {camera_name: snapshot_path} for the most recent snapshot per camera."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT e.camera_name, e.snapshot_path
            FROM detection_events e
            INNER JOIN (
                SELECT camera_name, MAX(id) AS max_id
                FROM detection_events
                WHERE snapshot_path IS NOT NULL
                  AND camera_name != '_system'
                GROUP BY camera_name
            ) latest ON e.id = latest.max_id
            """
        ).fetchall()
    return {row["camera_name"]: row["snapshot_path"] for row in rows}


def count_events_today() -> int:
    """Count detection events (excluding system events) since midnight UTC today."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM detection_events
            WHERE camera_name != '_system'
              AND timestamp >= strftime('%Y-%m-%dT00:00:00', 'now')
            """
        ).fetchone()
        return row[0] if row else 0


# ── Feedback ────────────────────────────────────────────────────────────────

_FEEDBACK_TOKEN_EXPIRY_DAYS = 7


def get_event_by_token(token: str) -> sqlite3.Row | None:
    """Look up a detection event by its feedback token.

    Returns None if the token is invalid or the event is older than the
    expiry window.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_FEEDBACK_TOKEN_EXPIRY_DAYS)).isoformat()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM detection_events
            WHERE feedback_token = ? AND timestamp >= ?
            """,
            (token, cutoff),
        ).fetchone()
        return row


def update_feedback(
    event_id: int,
    feedback: str,
    corrected_class: str | None = None,
) -> bool:
    """Set feedback on a detection event.  Returns True on success."""
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE detection_events
            SET feedback = ?, corrected_class = ?
            WHERE id = ?
            """,
            (feedback, corrected_class, event_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_feedback_stats(
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Aggregate feedback counts by class and feedback type.

    Returns::

        {
            "total_labeled": int,
            "total_unlabeled": int,
            "by_class": {
                "<class_name>": {"correct": N, "false_positive": N, "wrong_class": N}
            },
            "date_min": str | None,
            "date_max": str | None,
        }
    """
    where: list[str] = ["camera_name != '_system'"]
    params: list[object] = []
    if date_from:
        where.append("timestamp >= ?")
        params.append(date_from)
    if date_to:
        where.append("timestamp < ?")
        params.append(_date_to_exclusive(date_to))

    clause = "WHERE " + " AND ".join(where)

    with _connect() as conn:
        # Per-class feedback counts
        rows = conn.execute(
            f"""
            SELECT class_name, feedback, COUNT(*) AS cnt
            FROM detection_events
            {clause} AND feedback IS NOT NULL
            GROUP BY class_name, feedback
            """,
            params,
        ).fetchall()

        by_class: dict[str, dict[str, int]] = {}
        total_labeled = 0
        for r in rows:
            cls = r["class_name"]
            fb = r["feedback"]
            cnt = r["cnt"]
            total_labeled += cnt
            if cls not in by_class:
                by_class[cls] = {"correct": 0, "false_positive": 0, "wrong_class": 0}
            if fb in by_class[cls]:
                by_class[cls][fb] = cnt

        # Total unlabeled
        row = conn.execute(
            f"SELECT COUNT(*) FROM detection_events {clause} AND feedback IS NULL",
            params,
        ).fetchone()
        total_unlabeled = row[0] if row else 0

        # Date range coverage
        row = conn.execute(
            f"SELECT MIN(timestamp), MAX(timestamp) FROM detection_events {clause} AND feedback IS NOT NULL",
            params,
        ).fetchone()
        date_min = row[0] if row else None
        date_max = row[1] if row else None

    return {
        "total_labeled": total_labeled,
        "total_unlabeled": total_unlabeled,
        "by_class": by_class,
        "date_min": date_min,
        "date_max": date_max,
    }


# ── Export ──────────────────────────────────────────────────────────────────

def count_protected_events() -> int:
    """Count events with feedback labels (protected from pruning)."""
    conn = _connect()
    row = conn.execute(
        "SELECT COUNT(*) FROM detection_events"
        " WHERE feedback IS NOT NULL"
    ).fetchone()
    return row[0]


def count_pruneable_events() -> int:
    """Count unlabeled, non-system events eligible for pruning."""
    conn = _connect()
    row = conn.execute(
        "SELECT COUNT(*) FROM detection_events"
        " WHERE feedback IS NULL AND camera_name != '_system'"
    ).fetchone()
    return row[0]


_EXPORTABLE_WHERE = (
    "camera_name != '_system'"
    " AND feedback IN ('correct', 'wrong_class')"
    " AND bbox IS NOT NULL"
    " AND snapshot_path IS NOT NULL"
)


def count_exportable_events(
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    """Count events eligible for YOLO dataset export."""
    where = _EXPORTABLE_WHERE
    params: list[object] = []
    if date_from:
        where += " AND timestamp >= ?"
        params.append(date_from)
    if date_to:
        where += " AND timestamp < ?"
        params.append(_date_to_exclusive(date_to))
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM detection_events WHERE {where}", params
        ).fetchone()
        return row[0] if row else 0


def get_exportable_events(
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[sqlite3.Row]:
    """Return events eligible for YOLO dataset export."""
    where = _EXPORTABLE_WHERE
    params: list[object] = []
    if date_from:
        where += " AND timestamp >= ?"
        params.append(date_from)
    if date_to:
        where += " AND timestamp < ?"
        params.append(_date_to_exclusive(date_to))
    with _connect() as conn:
        return conn.execute(
            f"""
            SELECT id, class_name, confidence, camera_name,
                   snapshot_path, bbox, frame_size,
                   feedback, corrected_class
            FROM detection_events
            WHERE {where}
            ORDER BY id
            """,
            params,
        ).fetchall()


# ── Training nudge ─────────────────────────────────────────────────────────


def count_labeled_since(since_date: str | None) -> dict:
    """Count labeled events since a given date, grouped by class.

    Returns {"total": int, "by_class": {"class_name": count, ...}}
    """
    where = ["camera_name != '_system'", "feedback IS NOT NULL"]
    params: list[object] = []
    if since_date:
        where.append("timestamp >= ?")
        params.append(since_date)
    clause = "WHERE " + " AND ".join(where)

    try:
        with _connect() as conn:
            # Total count
            row = conn.execute(
                f"SELECT COUNT(*) FROM detection_events {clause}", params
            ).fetchone()
            total = row[0] if row else 0

            # By class
            rows = conn.execute(
                f"""
                SELECT class_name, COUNT(*) as cnt
                FROM detection_events
                {clause}
                GROUP BY class_name
                ORDER BY cnt DESC
                """,
                params,
            ).fetchall()
            by_class = {r["class_name"]: r["cnt"] for r in rows}

            return {"total": total, "by_class": by_class}
    except Exception:
        return {"total": 0, "by_class": {}}


def get_app_state(key: str) -> str | None:
    """Get a value from the app_state table."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None
    except Exception:
        return None


def set_app_state(key: str, value: str) -> None:
    """Set a value in the app_state table."""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()
    except Exception:
        pass


# ── Visits ──────────────────────────────────────────────────────────────────

def get_visits(
    limit: int = 100,
    offset: int = 0,
    camera: str | None = None,
    class_name: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[sqlite3.Row]:
    """Return visit sessions with optional filtering."""
    where: list[str] = []
    params: list[object] = []
    if camera:
        where.append("camera_name = ?")
        params.append(camera)
    if class_name:
        where.append("class_name = ?")
        params.append(class_name)
    if date_from:
        where.append("start_time >= ?")
        params.append(date_from)
    if date_to:
        where.append("end_time < ?")
        params.append(_date_to_exclusive(date_to))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params += [limit, offset]
    try:
        with _connect() as conn:
            return conn.execute(
                f"""
                SELECT id, camera_name, class_name, start_time, end_time,
                       duration_secs, detection_count
                FROM visit_sessions
                {clause}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
    except Exception:
        return []


def count_visits(
    camera: str | None = None,
    class_name: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    where: list[str] = []
    params: list[object] = []
    if camera:
        where.append("camera_name = ?")
        params.append(camera)
    if class_name:
        where.append("class_name = ?")
        params.append(class_name)
    if date_from:
        where.append("start_time >= ?")
        params.append(date_from)
    if date_to:
        where.append("end_time < ?")
        params.append(_date_to_exclusive(date_to))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    try:
        with _connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM visit_sessions {clause}", params
            ).fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


# ── Metrics ────────────────────────────────────────────────────────────────


def get_metrics(
    range_hours: int = 24,
    limit: int = 5000,
) -> list[sqlite3.Row]:
    """Return system metrics samples from the last *range_hours* hours."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=range_hours)
    ).isoformat()
    try:
        with _connect() as conn:
            # Sub-select newest rows first, then re-order ascending for charts.
            # Without this, LIMIT would keep the oldest rows and drop the most
            # recent data once a range exceeds the cap.
            return conn.execute(
                """
                SELECT * FROM (
                    SELECT timestamp, cpu_pct, gpu_pct, gpu_temp,
                           ram_used_mb, ram_total_mb, camera_data
                    FROM system_metrics
                    WHERE timestamp >= ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                ) ORDER BY timestamp ASC
                """,
                (cutoff, limit),
            ).fetchall()
    except Exception:
        return []


# Target number of data points for chart display.
_CHART_TARGET_POINTS = 2000
# Collection interval in seconds (must match stats_collector).
_COLLECTION_INTERVAL = 5


def get_metrics_for_chart(range_hours: int = 24) -> list[sqlite3.Row]:
    """Return metrics for chart display, downsampled for large ranges.

    For short ranges (≤ ~2.7 h at 5 s intervals) raw data is returned.
    For longer ranges, data is aggregated into time buckets so the response
    stays around ~2000 data points regardless of range.
    """
    raw_point_count = (range_hours * 3600) // _COLLECTION_INTERVAL
    if raw_point_count <= _CHART_TARGET_POINTS:
        return get_metrics(range_hours=range_hours, limit=_CHART_TARGET_POINTS)

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=range_hours)
    ).isoformat()
    bucket_seconds = (range_hours * 3600) // _CHART_TARGET_POINTS

    try:
        with _connect() as conn:
            return conn.execute(
                """
                SELECT MIN(timestamp) AS timestamp,
                       ROUND(AVG(cpu_pct), 1)  AS cpu_pct,
                       ROUND(AVG(gpu_pct), 1)  AS gpu_pct,
                       ROUND(AVG(gpu_temp), 1) AS gpu_temp,
                       ROUND(AVG(ram_used_mb))  AS ram_used_mb,
                       MAX(ram_total_mb)        AS ram_total_mb,
                       NULL                     AS camera_data
                FROM system_metrics
                WHERE timestamp >= ?
                GROUP BY CAST(strftime('%s', timestamp) / ? AS INTEGER)
                ORDER BY timestamp ASC
                """,
                (cutoff, bucket_seconds),
            ).fetchall()
    except Exception:
        return []
