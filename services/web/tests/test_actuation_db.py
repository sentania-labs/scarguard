"""Tests for services/web/src/actuation_db.py (read-only view of
deterrent SQLite)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import actuation_db


@pytest.fixture
def empty_db(tmp_path: Path, monkeypatch) -> Path:
    """An empty file with the full v0.13.3 schema present."""
    db = tmp_path / "deterrent.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE actuation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            trigger_class TEXT NOT NULL,
            trigger_camera TEXT NOT NULL,
            trigger_confidence REAL NOT NULL,
            pre_delay_sec REAL NOT NULL,
            total_duration_sec REAL NOT NULL,
            device_count INTEGER NOT NULL,
            success_count INTEGER NOT NULL,
            group_name TEXT NOT NULL DEFAULT '',
            trigger_delay_ms REAL,
            queue_depth INTEGER
        );
        CREATE TABLE device_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL REFERENCES actuation_events(id),
            device_name TEXT NOT NULL,
            device_id TEXT NOT NULL,
            device_type TEXT NOT NULL,
            duration_sec REAL NOT NULL,
            delay_before_sec REAL NOT NULL,
            success INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            cloud_ack_ms REAL
        );
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(actuation_db, "DB_PATH", str(db))
    return db


def _insert_event(
    db_path: Path,
    *,
    ts: str = "2026-04-20T10:00:00",
    trigger_delay_ms: float | None = None,
    total_duration_sec: float = 1.0,
    actions: list[tuple[str, float | None]] | None = None,
) -> int:
    """Insert a minimal actuation row + optional device actions.

    ``actions`` is a list of (device_name, cloud_ack_ms) pairs."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO actuation_events "
        "(timestamp, trigger_class, trigger_camera, trigger_confidence, "
        " pre_delay_sec, total_duration_sec, device_count, success_count, "
        " group_name, trigger_delay_ms, queue_depth) "
        "VALUES (?, 'bird', 'pond', 0.9, 0.0, ?, ?, ?, 'g', ?, 0)",
        (ts, total_duration_sec, len(actions or []), len(actions or []),
         trigger_delay_ms),
    )
    event_id = cur.lastrowid
    for name, ack in (actions or []):
        conn.execute(
            "INSERT INTO device_actions "
            "(event_id, device_name, device_id, device_type, duration_sec, "
            " delay_before_sec, success, error, cloud_ack_ms) "
            "VALUES (?, ?, 'x', 'sprinkler', 1.0, 0.0, 1, NULL, ?)",
            (event_id, name, ack),
        )
    conn.commit()
    conn.close()
    assert event_id is not None
    return event_id


def test_latency_summary_empty_returns_nulls(empty_db):
    out = actuation_db.get_latency_summary()
    assert out["count"] == 0
    assert out["trigger_delay_ms"] == {"p50": None, "p95": None}


def test_latency_summary_basic_percentiles(empty_db):
    # Insert 5 events: trigger_delay 100, 200, 300, 400, 500 ms
    for i, delay in enumerate([100, 200, 300, 400, 500]):
        _insert_event(
            empty_db,
            ts=f"2026-04-20T10:0{i}:00",
            trigger_delay_ms=float(delay),
            total_duration_sec=1.0 + i * 0.5,
            actions=[("dev", float(delay / 2))],
        )
    out = actuation_db.get_latency_summary(last_n=10)
    assert out["count"] == 5
    assert out["trigger_delay_ms"]["p50"] == pytest.approx(300.0)
    # p95 over 5 samples (linear interp): 500 at idx 4, k = 4*0.95 = 3.8 →
    # 0.8 of the way from 400 to 500 → 480
    assert out["trigger_delay_ms"]["p95"] == pytest.approx(480.0)
    assert out["cloud_ack_ms"]["p50"] == pytest.approx(150.0)


def test_latency_summary_missing_values_excluded(empty_db):
    _insert_event(empty_db, trigger_delay_ms=None, actions=[("dev", None)])
    _insert_event(empty_db, trigger_delay_ms=200.0, actions=[("dev", 50.0)])
    out = actuation_db.get_latency_summary()
    assert out["count"] == 2
    # Only one row has a non-null trigger_delay_ms; p50 == p95 == 200
    assert out["trigger_delay_ms"]["p50"] == pytest.approx(200.0)
    assert out["cloud_ack_ms"]["p50"] == pytest.approx(50.0)


def test_latency_summary_missing_db_file(tmp_path, monkeypatch):
    monkeypatch.setattr(actuation_db, "DB_PATH", str(tmp_path / "nope.db"))
    out = actuation_db.get_latency_summary()
    assert out["count"] == 0
