"""Read-only SQLite queries for digest report generation."""

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/scarguard.db")
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/data/snapshots")
MODELS_DIR = os.environ.get("MODELS_DIR", "/models")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def count_events_by_class(since: datetime) -> dict[str, int]:
    """Detection counts grouped by class name."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT class_name, COUNT(*) as cnt FROM detection_events"
            " WHERE timestamp >= ? AND camera_name != '_system'"
            " GROUP BY class_name ORDER BY cnt DESC",
            (since.isoformat(),),
        ).fetchall()
        return {r["class_name"]: r["cnt"] for r in rows}
    finally:
        conn.close()


def count_events_by_camera(since: datetime) -> dict[str, int]:
    """Detection counts grouped by camera name."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT camera_name, COUNT(*) as cnt FROM detection_events"
            " WHERE timestamp >= ? AND camera_name != '_system'"
            " GROUP BY camera_name ORDER BY cnt DESC",
            (since.isoformat(),),
        ).fetchall()
        return {r["camera_name"]: r["cnt"] for r in rows}
    finally:
        conn.close()


def total_events(since: datetime, until: datetime | None = None) -> int:
    """Total detection events in period."""
    conn = _connect()
    try:
        if until is not None:
            row = conn.execute(
                "SELECT COUNT(*) FROM detection_events"
                " WHERE timestamp >= ? AND timestamp < ?"
                " AND camera_name != '_system'",
                (since.isoformat(), until.isoformat()),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM detection_events"
                " WHERE timestamp >= ? AND camera_name != '_system'",
                (since.isoformat(),),
            ).fetchone()
        return row[0]
    finally:
        conn.close()


def top_visits(since: datetime, limit: int = 3) -> list[dict]:
    """Longest visit sessions in period."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT camera_name, class_name, duration_secs, detection_count,"
            " start_time, end_time"
            " FROM visit_sessions WHERE start_time >= ?"
            " ORDER BY duration_secs DESC LIMIT ?",
            (since.isoformat(), limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def total_visits(since: datetime) -> int:
    """Total visit sessions in period."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM visit_sessions WHERE start_time >= ?",
            (since.isoformat(),),
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def feedback_summary(since: datetime) -> dict[str, int]:
    """Feedback label counts: correct, false_positive, wrong_class, unlabeled."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT feedback, COUNT(*) as cnt FROM detection_events"
            " WHERE timestamp >= ? AND camera_name != '_system'"
            " GROUP BY feedback",
            (since.isoformat(),),
        ).fetchall()
        result: dict[str, int] = {}
        for r in rows:
            key = r["feedback"] if r["feedback"] else "unlabeled"
            result[key] = r["cnt"]
        return result
    finally:
        conn.close()


def avg_metrics(since: datetime) -> dict[str, float | None]:
    """Average CPU%, GPU%, GPU temp over period."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT AVG(cpu_pct) as cpu, AVG(gpu_pct) as gpu,"
            " AVG(gpu_temp) as temp"
            " FROM system_metrics WHERE timestamp >= ?",
            (since.isoformat(),),
        ).fetchone()
        return {
            "cpu_pct": round(row["cpu"], 1) if row["cpu"] is not None else None,
            "gpu_pct": round(row["gpu"], 1) if row["gpu"] is not None else None,
            "gpu_temp": round(row["temp"], 1) if row["temp"] is not None else None,
        }
    finally:
        conn.close()


def camera_offline_count(since: datetime) -> dict[str, int]:
    """Count of camera_offline events per camera."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT camera_name, COUNT(*) as cnt FROM detection_events"
            " WHERE timestamp >= ? AND class_name = 'camera_offline'"
            " GROUP BY camera_name",
            (since.isoformat(),),
        ).fetchall()
        return {r["camera_name"]: r["cnt"] for r in rows}
    finally:
        conn.close()


def protected_event_count() -> int:
    """Events with feedback labels (protected from pruning)."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM detection_events WHERE feedback IS NOT NULL"
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def pruneable_event_count() -> int:
    """Unlabeled, non-system events eligible for pruning."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM detection_events"
            " WHERE feedback IS NULL AND camera_name != '_system'"
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def dir_size_mb(path: str) -> float:
    """Total size of directory in MB."""
    try:
        total = sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
        return round(total / (1024 * 1024), 1)
    except OSError:
        return 0.0


def db_size_mb() -> float:
    """SQLite database file size in MB."""
    try:
        return round(Path(DB_PATH).stat().st_size / (1024 * 1024), 1)
    except OSError:
        return 0.0


def storage_summary() -> dict[str, float]:
    """Storage sizes in MB."""
    snap = dir_size_mb(SNAPSHOT_DIR)
    db = db_size_mb()
    models = dir_size_mb(MODELS_DIR)
    return {
        "snapshots_mb": snap,
        "database_mb": db,
        "models_mb": models,
        "total_mb": round(snap + db + models, 1),
    }
