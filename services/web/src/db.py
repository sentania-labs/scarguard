"""SQLite access for detection events.

The detector service is the primary writer (INSERTs).  The web service writes
only to the ``feedback`` and ``corrected_class`` columns via UPDATE.
"""

import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

DB_PATH = os.environ.get("DB_PATH", "/data/scarguard.db")


# ── Training tables ──────────────────────────────────────────────────────────


def _add_column_if_missing(conn: sqlite3.Connection, table: str, col_name: str, col_def: str) -> None:
    """Idempotent ALTER TABLE ADD COLUMN — SQLite has no IF NOT EXISTS for ALTER."""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col_name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")


def ensure_training_tables() -> None:
    """Create the training pipeline tables if they don't exist yet."""
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS training_uploads (
                id                TEXT    PRIMARY KEY,
                filename          TEXT    NOT NULL,
                target_class_hint TEXT,
                frame_count       INTEGER,
                detection_count   INTEGER,
                status            TEXT    NOT NULL DEFAULT 'uploaded',
                error             TEXT,
                created_at        TEXT    NOT NULL,
                processed_at      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tu_status
                ON training_uploads(status);

            CREATE TABLE IF NOT EXISTS training_events (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_id         TEXT    NOT NULL REFERENCES training_uploads(id) ON DELETE CASCADE,
                frame_idx         INTEGER NOT NULL,
                timestamp_in_video REAL,
                bbox              TEXT    NOT NULL,
                predicted_class   TEXT    NOT NULL,
                confidence        REAL    NOT NULL,
                target_class_hint TEXT,
                detection_pass    TEXT    NOT NULL,
                review_state      TEXT    NOT NULL DEFAULT 'pending',
                corrected_class   TEXT,
                created_at        TEXT    NOT NULL,
                reviewed_at       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_te_upload
                ON training_events(upload_id);
            CREATE INDEX IF NOT EXISTS idx_te_review
                ON training_events(review_state);
            CREATE INDEX IF NOT EXISTS idx_te_pass
                ON training_events(detection_pass);
            CREATE INDEX IF NOT EXISTS idx_te_frame
                ON training_events(upload_id, frame_idx);

            CREATE TABLE IF NOT EXISTS training_jobs (
                id            TEXT    PRIMARY KEY,
                type          TEXT    NOT NULL,
                params        TEXT    NOT NULL,
                status        TEXT    NOT NULL DEFAULT 'queued',
                result        TEXT,
                created_at    TEXT    NOT NULL,
                started_at    TEXT,
                completed_at  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tj_status
                ON training_jobs(status);
        """)
        # Additive columns on training_uploads — let existing rows default to
        # NULL so the trainer falls back to global config.
        _add_column_if_missing(conn, "training_uploads", "detector_model", "detector_model TEXT")
        _add_column_if_missing(conn, "training_uploads", "confidence_threshold", "confidence_threshold REAL")
        _add_column_if_missing(conn, "training_uploads", "hints", "hints TEXT")
        # Human-drawn replacement annotations for a frame. JSON list of
        # {"cls": str, "bbox": [xc, yc, w, h]} where bbox is normalized.
        # When set on a 'corrected' event, the exporter emits one YOLO row
        # per entry instead of the detector's original bbox+predicted_class.
        _add_column_if_missing(conn, "training_events", "corrected_bboxes", "corrected_bboxes TEXT")
        # Durable while-running execution evidence survives a trainer crash;
        # final result JSON contains the completed metadata as well.
        _add_column_if_missing(
            conn, "training_jobs", "execution_metadata", "execution_metadata TEXT"
        )
        conn.commit()


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
                   feedback, corrected_class, corrected_bbox
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
    corrected_bbox: str | None = None,
) -> bool:
    """Set feedback on a detection event.  Returns True on success."""
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE detection_events
            SET feedback = ?, corrected_class = ?, corrected_bbox = ?
            WHERE id = ?
            """,
            (feedback, corrected_class, corrected_bbox, event_id),
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
        # Per-class feedback counts.  Group by *effective* class so a
        # wrong-class correction (e.g. model said 'person', user labelled
        # 'heron') shows up under the corrected label, matching what the
        # YOLO export will use as the training class.
        rows = conn.execute(
            f"""
            SELECT
                CASE
                    WHEN feedback = 'wrong_class'
                         AND corrected_class IS NOT NULL
                         AND corrected_class != ''
                    THEN corrected_class
                    ELSE class_name
                END AS effective_class,
                feedback,
                COUNT(*) AS cnt
            FROM detection_events
            {clause} AND feedback IS NOT NULL
            GROUP BY effective_class, feedback
            """,
            params,
        ).fetchall()

        by_class: dict[str, dict[str, int]] = {}
        total_labeled = 0
        for r in rows:
            cls = r["effective_class"]
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


# Positives drive the dashboard "X exportable" count and the trainable
# class definitions in data.yaml.
_EXPORTABLE_POSITIVE_WHERE = (
    "camera_name != '_system'"
    " AND feedback IN ('correct', 'wrong_class')"
    " AND bbox IS NOT NULL"
    " AND snapshot_path IS NOT NULL"
)

# Exportable rows = positives + false positives (the latter ride along as
# YOLO background samples — image + empty label file).  FPs without a
# bbox are still valid backgrounds, so the bbox filter is positives-only.
_EXPORTABLE_WHERE = (
    "camera_name != '_system'"
    " AND feedback IN ('correct', 'wrong_class', 'false_positive')"
    " AND snapshot_path IS NOT NULL"
    " AND ("
    "feedback = 'false_positive'"
    " OR bbox IS NOT NULL"
    ")"
)


def count_exportable_events(
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    """Count *positive* events that drive the trainable class definitions
    in the export.  False positives ride along in the zip as background
    samples (see ``get_exportable_events``) but they don't contribute a
    class to data.yaml, so the dashboard "X exportable" headline counts
    positives only.  Otherwise the UI would advertise an FP-only range as
    exportable while the export endpoint correctly rejects that case as a
    degenerate dataset.
    """
    where = _EXPORTABLE_POSITIVE_WHERE
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
                   feedback, corrected_class, corrected_bbox
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


def get_metrics_for_chart(
    range_hours: int = 24,
    collection_interval: int = 5,
) -> list[Any]:
    """Return metrics for chart display, downsampled for large ranges.

    *collection_interval* is the stats collection cadence in seconds
    (from ``system.stats_interval`` config).  It determines whether the
    raw or downsampled path is used.

    For short ranges where raw point count fits in ~2000, raw data is
    returned.  For longer ranges, data is aggregated into time buckets.
    """
    raw_point_count = (range_hours * 3600) // max(collection_interval, 1)
    if raw_point_count <= _CHART_TARGET_POINTS:
        return get_metrics(range_hours=range_hours, limit=_CHART_TARGET_POINTS)

    now = datetime.now(timezone.utc)
    cutoff_dt = now - timedelta(hours=range_hours)
    cutoff = cutoff_dt.isoformat()
    bucket_seconds = (range_hours * 3600) // _CHART_TARGET_POINTS

    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT CAST(strftime('%s', timestamp) / ? AS INTEGER) AS bucket_id,
                       MIN(timestamp) AS timestamp,
                       ROUND(AVG(cpu_pct), 1)  AS cpu_pct,
                       ROUND(AVG(gpu_pct), 1)  AS gpu_pct,
                       ROUND(AVG(gpu_temp), 1) AS gpu_temp,
                       ROUND(AVG(ram_used_mb))  AS ram_used_mb,
                       MAX(ram_total_mb)        AS ram_total_mb,
                       MAX(camera_data)         AS camera_data
                FROM system_metrics
                WHERE timestamp >= ?
                GROUP BY bucket_id
                ORDER BY timestamp ASC
                """,
                (bucket_seconds, cutoff),
            ).fetchall()
    except Exception:
        return []

    # Fill missing buckets with null-valued placeholders so the frontend
    # chart can render gaps as breaks instead of interpolating straight
    # lines across multi-hour or multi-day downtime.  Without this, a
    # `GROUP BY` bucket with zero rows simply does not appear in the
    # result, and Chart.js draws a line between the two neighbouring
    # populated buckets.  That was the "invents data" symptom in #93.
    start_bucket = int(cutoff_dt.timestamp() // bucket_seconds)
    end_bucket = int(now.timestamp() // bucket_seconds)
    by_bucket: dict[int, sqlite3.Row] = {r["bucket_id"]: r for r in rows}

    filled: list[Any] = []
    for b in range(start_bucket, end_bucket + 1):
        if b in by_bucket:
            filled.append(by_bucket[b])
            continue
        ts = datetime.fromtimestamp(b * bucket_seconds, tz=timezone.utc).isoformat()
        filled.append({
            "timestamp": ts,
            "cpu_pct": None,
            "gpu_pct": None,
            "gpu_temp": None,
            "ram_used_mb": None,
            "ram_total_mb": None,
            "camera_data": None,
        })
    return filled


# ── Training uploads ─────────────────────────────────────────────────────────


def create_training_upload(
    upload_id: str,
    filename: str,
    target_class_hint: str | None,
    *,
    detector_model: str | None = None,
    confidence_threshold: float | None = None,
    hints: str | None = None,
) -> None:
    """INSERT a new training_uploads row with status='uploaded'.

    ``hints`` is a JSON-encoded list of class names — the multi-value
    superset of the legacy ``target_class_hint`` single value. Both are
    kept in sync at write-time so older readers keep working.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO training_uploads
                (id, filename, target_class_hint, detector_model,
                 confidence_threshold, hints, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'uploaded', ?)
            """,
            (
                upload_id, filename, target_class_hint,
                detector_model, confidence_threshold, hints, now,
            ),
        )
        conn.commit()


def update_training_upload_settings(
    upload_id: str,
    *,
    target_class_hint: str | None,
    detector_model: str | None,
    confidence_threshold: float | None,
    hints: str | None,
) -> bool:
    """UPDATE the user-editable settings on a training upload."""
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE training_uploads
            SET target_class_hint = ?,
                detector_model = ?,
                confidence_threshold = ?,
                hints = ?
            WHERE id = ?
            """,
            (target_class_hint, detector_model, confidence_threshold, hints, upload_id),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_training_events_for_upload(upload_id: str) -> int:
    """DELETE all training_events rows for an upload. Returns rows deleted."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM training_events WHERE upload_id = ?", (upload_id,)
        )
        conn.commit()
        return cur.rowcount


def get_training_uploads(
    limit: int = 100,
    offset: int = 0,
    status: str | None = None,
) -> list[sqlite3.Row]:
    """List training uploads, newest first."""
    where: list[str] = []
    params: list[object] = []
    if status:
        where.append("status = ?")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params += [limit, offset]
    with _connect() as conn:
        return conn.execute(
            f"""
            SELECT * FROM training_uploads
            {clause}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()


def count_training_uploads(status: str | None = None) -> int:
    where: list[str] = []
    params: list[object] = []
    if status:
        where.append("status = ?")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM training_uploads {clause}", params
        ).fetchone()
        return row[0] if row else 0


def get_training_upload(upload_id: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM training_uploads WHERE id = ?", (upload_id,)
        ).fetchone()


def update_training_upload_status(
    upload_id: str,
    status: str,
    *,
    frame_count: int | None = None,
    detection_count: int | None = None,
    error: str | None = None,
) -> bool:
    sets = ["status = ?"]
    params: list[object] = [status]
    if frame_count is not None:
        sets.append("frame_count = ?")
        params.append(frame_count)
    if detection_count is not None:
        sets.append("detection_count = ?")
        params.append(detection_count)
    if error is not None:
        sets.append("error = ?")
        params.append(error)
    if status in ("processed", "failed"):
        sets.append("processed_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
    params.append(upload_id)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE training_uploads SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()
        return cur.rowcount > 0


def delete_training_upload(upload_id: str) -> bool:
    """DELETE upload and its events (manual cascade — avoids PRAGMA foreign_keys)."""
    with _connect() as conn:
        conn.execute("DELETE FROM training_events WHERE upload_id = ?", (upload_id,))
        cur = conn.execute("DELETE FROM training_uploads WHERE id = ?", (upload_id,))
        conn.commit()
        return cur.rowcount > 0


# ── Training events ──────────────────────────────────────────────────────────


def insert_training_events(upload_id: str, events: list[dict]) -> int:
    """Bulk-insert training events. Returns rows inserted."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO training_events
                (upload_id, frame_idx, timestamp_in_video, bbox, predicted_class,
                 confidence, target_class_hint, detection_pass, review_state, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            [
                (
                    upload_id,
                    e["frame_idx"],
                    e.get("timestamp_in_video"),
                    e["bbox"] if isinstance(e["bbox"], str) else __import__("json").dumps(e["bbox"]),
                    e["predicted_class"],
                    e["confidence"],
                    e.get("target_class_hint"),
                    e["detection_pass"],
                    now,
                )
                for e in events
            ],
        )
        conn.commit()
        return len(events)


def get_training_events(
    upload_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
    review_state: str | None = None,
    detection_pass: str | None = None,
) -> list[sqlite3.Row]:
    where = ["upload_id = ?"]
    params: list[object] = [upload_id]
    if review_state:
        where.append("review_state = ?")
        params.append(review_state)
    if detection_pass:
        where.append("detection_pass = ?")
        params.append(detection_pass)
    clause = "WHERE " + " AND ".join(where)
    params += [limit, offset]
    with _connect() as conn:
        return conn.execute(
            f"""
            SELECT * FROM training_events
            {clause}
            ORDER BY confidence DESC, id ASC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()


def count_training_events(
    upload_id: str,
    *,
    review_state: str | None = None,
    detection_pass: str | None = None,
) -> int:
    where = ["upload_id = ?"]
    params: list[object] = [upload_id]
    if review_state:
        where.append("review_state = ?")
        params.append(review_state)
    if detection_pass:
        where.append("detection_pass = ?")
        params.append(detection_pass)
    clause = "WHERE " + " AND ".join(where)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM training_events {clause}", params
        ).fetchone()
        return row[0] if row else 0


def get_training_event(event_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM training_events WHERE id = ?", (event_id,)
        ).fetchone()


def get_next_training_event(
    upload_id: str,
    current_id: int,
    *,
    review_state: str | None = None,
    detection_pass: str | None = None,
) -> sqlite3.Row | None:
    """Get the next event (by confidence desc, id asc) after current_id."""
    where = ["upload_id = ?", "(confidence < (SELECT confidence FROM training_events WHERE id = ?) OR (confidence = (SELECT confidence FROM training_events WHERE id = ?) AND id > ?))"]
    params: list[object] = [upload_id, current_id, current_id, current_id]
    if review_state:
        where.append("review_state = ?")
        params.append(review_state)
    if detection_pass:
        where.append("detection_pass = ?")
        params.append(detection_pass)
    clause = "WHERE " + " AND ".join(where)
    with _connect() as conn:
        return conn.execute(
            f"""
            SELECT * FROM training_events
            {clause}
            ORDER BY confidence DESC, id ASC
            LIMIT 1
            """,
            params,
        ).fetchone()


def get_prev_training_event(
    upload_id: str,
    current_id: int,
    *,
    review_state: str | None = None,
    detection_pass: str | None = None,
) -> sqlite3.Row | None:
    """Get the previous event (by confidence desc, id asc) before current_id."""
    where = ["upload_id = ?", "(confidence > (SELECT confidence FROM training_events WHERE id = ?) OR (confidence = (SELECT confidence FROM training_events WHERE id = ?) AND id < ?))"]
    params: list[object] = [upload_id, current_id, current_id, current_id]
    if review_state:
        where.append("review_state = ?")
        params.append(review_state)
    if detection_pass:
        where.append("detection_pass = ?")
        params.append(detection_pass)
    clause = "WHERE " + " AND ".join(where)
    with _connect() as conn:
        return conn.execute(
            f"""
            SELECT * FROM training_events
            {clause}
            ORDER BY confidence ASC, id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()


def update_training_event_review(
    event_id: int,
    review_state: str,
    corrected_class: str | None = None,
    corrected_bboxes: str | None = None,
) -> bool:
    """Update review_state, optional corrected_class, optional corrected_bboxes.

    ``corrected_bboxes`` is a JSON-encoded list of
    ``{"cls": str, "bbox": [xc, yc, w, h]}`` (normalized 0-1) that overrides
    the detector's prediction at export time.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE training_events
            SET review_state = ?, corrected_class = ?, corrected_bboxes = ?,
                reviewed_at = ?
            WHERE id = ?
            """,
            (review_state, corrected_class, corrected_bboxes, now, event_id),
        )
        conn.commit()
        return cur.rowcount > 0


def insert_manual_training_event(
    upload_id: str,
    frame_idx: int,
    corrected_bboxes_json: str,
) -> int:
    """Insert a human-annotated training event for a frame that had no detector
    output. Stores sentinel values for the NOT NULL columns; the exporter
    treats ``review_state='corrected'`` + ``corrected_bboxes`` as the
    authoritative annotation regardless of the (empty) predicted class.

    Returns the new event id.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO training_events
                (upload_id, frame_idx, timestamp_in_video, bbox, predicted_class,
                 confidence, target_class_hint, detection_pass, review_state,
                 corrected_bboxes, created_at, reviewed_at)
            VALUES (?, ?, NULL, '[]', '', 0.0, NULL, 'manual', 'corrected',
                    ?, ?, ?)
            """,
            (upload_id, frame_idx, corrected_bboxes_json, now, now),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def get_training_events_by_frames(
    upload_id: str,
    frame_idxs: list[int],
) -> dict[int, list[sqlite3.Row]]:
    """Fetch events for a specific set of frames, grouped by frame_idx.

    Used by the frame-browser grid to badge thumbnails ("has detection",
    "has annotation") without issuing one query per tile.
    """
    if not frame_idxs:
        return {}
    placeholders = ",".join("?" * len(frame_idxs))
    out: dict[int, list[sqlite3.Row]] = {}
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM training_events
            WHERE upload_id = ?
              AND frame_idx IN ({placeholders})
            """,
            [upload_id, *frame_idxs],
        ).fetchall()
    for r in rows:
        out.setdefault(r["frame_idx"], []).append(r)
    return out


def bulk_update_training_events(
    upload_id: str,
    review_state: str,
    *,
    filter_pass: str | None = None,
    filter_review_state: str | None = None,
) -> int:
    """Bulk update review_state. Returns rows affected."""
    now = datetime.now(timezone.utc).isoformat()
    where = ["upload_id = ?"]
    params: list[object] = [review_state, now, upload_id]
    if filter_pass:
        where.append("detection_pass = ?")
        params.append(filter_pass)
    if filter_review_state:
        where.append("review_state = ?")
        params.append(filter_review_state)
    clause = "WHERE " + " AND ".join(where)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE training_events SET review_state = ?, reviewed_at = ? {clause}",
            params,
        )
        conn.commit()
        return cur.rowcount


def get_training_upload_stats(upload_id: str) -> dict:
    """Return review state counts for an upload."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT review_state, COUNT(*) as cnt
            FROM training_events
            WHERE upload_id = ?
            GROUP BY review_state
            """,
            (upload_id,),
        ).fetchall()
    stats = {"pending": 0, "approved": 0, "rejected": 0, "corrected": 0, "total": 0}
    for r in rows:
        state = r["review_state"]
        cnt = r["cnt"]
        if state in stats:
            stats[state] = cnt
        stats["total"] += cnt
    return stats


def get_exportable_training_events() -> list[sqlite3.Row]:
    """Return approved/corrected training events for dataset export."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT te.*, tu.target_class_hint AS upload_hint
            FROM training_events te
            JOIN training_uploads tu ON te.upload_id = tu.id
            WHERE te.review_state IN ('approved', 'corrected')
            ORDER BY te.upload_id, te.frame_idx
            """
        ).fetchall()


# ── Training jobs ────────────────────────────────────────────────────────────


def create_training_job(
    job_id: str,
    job_type: str,
    params: str,
) -> None:
    """INSERT a new training_jobs row with status='queued'."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO training_jobs (id, type, params, status, created_at)
            VALUES (?, ?, ?, 'queued', ?)
            """,
            (job_id, job_type, params, now),
        )
        conn.commit()


def get_training_jobs(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list[object] = []
    if status:
        where.append("status = ?")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params += [limit, offset]
    with _connect() as conn:
        return conn.execute(
            f"""
            SELECT * FROM training_jobs
            {clause}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()


def count_training_jobs(status: str | None = None) -> int:
    where: list[str] = []
    params: list[object] = []
    if status:
        where.append("status = ?")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM training_jobs {clause}", params
        ).fetchone()
        return row[0] if row else 0


def get_training_job(job_id: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM training_jobs WHERE id = ?", (job_id,)
        ).fetchone()


def get_oldest_queued_job() -> sqlite3.Row | None:
    """Return the oldest queued training job, or None."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM training_jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()


def update_training_job_status(
    job_id: str,
    status: str,
    *,
    result: str | None = None,
) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    sets = ["status = ?"]
    params: list[object] = [status]
    if status == "running":
        sets.append("started_at = ?")
        params.append(now)
    if status in ("completed", "failed", "cancelled"):
        sets.append("completed_at = ?")
        params.append(now)
    if result is not None:
        sets.append("result = ?")
        params.append(result)
    params.append(job_id)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE training_jobs SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()
        return cur.rowcount > 0


def mark_stale_running_jobs_failed() -> int:
    """Mark any running jobs as failed (crash recovery on trainer restart)."""
    now = datetime.now(timezone.utc).isoformat()
    result_json = __import__("json").dumps({"error": "Trainer restarted — job interrupted"})
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE training_jobs SET status = 'failed', completed_at = ?, result = ? WHERE status = 'running'",
            (now, result_json),
        )
        conn.commit()
        return cur.rowcount
