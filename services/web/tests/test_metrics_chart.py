"""Regression tests for `db.get_metrics_for_chart` null-bucket fill.

Fix for scarguard issue #93 — historical stats charts over multi-day
ranges connected a straight line across downtime because the
`GROUP BY` bucketing query omits empty buckets entirely.  The fix
emits null-valued placeholder rows for missing buckets so the frontend
renders the gap as a break (Chart.js defaults spanGaps to false).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def metrics_db(tmp_path, monkeypatch):
    """Build a fresh SQLite DB with the system_metrics schema and point
    the `db` module at it.
    """
    path = tmp_path / "metrics.db"
    monkeypatch.setenv("DB_PATH", str(path))

    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE system_metrics (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    TEXT    NOT NULL,
            cpu_pct      REAL,
            gpu_pct      REAL,
            gpu_temp     REAL,
            ram_used_mb  INTEGER,
            ram_total_mb INTEGER,
            camera_data  TEXT
        )
        """
    )
    conn.commit()

    # Reload the db module so it picks up the new DB_PATH.
    import importlib

    import db as db_module

    importlib.reload(db_module)
    yield conn, db_module
    conn.close()


def _insert(conn: sqlite3.Connection, ts: datetime, cpu: float) -> None:
    conn.execute(
        "INSERT INTO system_metrics (timestamp, cpu_pct, gpu_pct, gpu_temp, "
        "ram_used_mb, ram_total_mb, camera_data) VALUES (?, ?, 10.0, 40.0, 100, 1000, NULL)",
        (ts.isoformat(), cpu),
    )
    conn.commit()


def test_missing_buckets_are_null_filled(metrics_db):
    conn, db_module = metrics_db

    now = datetime.now(timezone.utc)
    # Two real samples 24h apart within a 7d window.
    _insert(conn, now - timedelta(hours=1), 42.0)
    _insert(conn, now - timedelta(hours=25), 55.0)

    rows = db_module.get_metrics_for_chart(range_hours=7 * 24, collection_interval=5)

    # Expect roughly _CHART_TARGET_POINTS buckets covering the whole window.
    assert len(rows) > 100, "should emit a bucket per time slot across the full range"

    # At least one bucket has a real CPU value, and many buckets are null.
    real = [r for r in rows if r["cpu_pct"] is not None]
    nulls = [r for r in rows if r["cpu_pct"] is None]
    assert len(real) >= 1
    assert len(nulls) >= 10, "downtime buckets should be null-filled, not omitted"

    # Timestamps are monotonic ascending.
    ts_list = [r["timestamp"] for r in rows]
    assert ts_list == sorted(ts_list)


def test_all_empty_range_still_returns_bucket_grid(metrics_db):
    _, db_module = metrics_db
    rows = db_module.get_metrics_for_chart(range_hours=30 * 24, collection_interval=5)
    # Fresh DB, zero data — should return a full grid of null buckets
    # rather than an empty list (so the chart x-axis spans the whole
    # requested range and the "blank when no data" behaviour from #93 is
    # handled by the frontend's data.length === 0 check on all-null data
    # being separate).
    assert len(rows) > 100
    assert all(r["cpu_pct"] is None for r in rows)
