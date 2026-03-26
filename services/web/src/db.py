"""SQLite access for detection events.

The detector service is the primary writer (INSERTs).  The web service writes
only to the ``feedback`` and ``corrected_class`` columns via UPDATE.
"""

import os
import sqlite3
from datetime import date, timedelta

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
