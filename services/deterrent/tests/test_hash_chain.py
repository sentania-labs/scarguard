"""Tests for the tamper-evident HMAC hash chain on actuation_events.

Exercises init_db migration, insert_event chaining, verify_chain
validation, and graceful degradation when the HMAC key is absent.
"""

from __future__ import annotations

import base64
import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest
from actuation_db import (
    _compute_row_hash,
    _get_conn,
    _local,
    init_db,
    insert_event,
    verify_chain,
)
from actuation_models import ActuationEvent, DeviceAction

# A deterministic 32-byte key for testing.
_TEST_KEY = os.urandom(32)
_TEST_KEY_B64 = base64.b64encode(_TEST_KEY).decode()


def _make_event(
    ts: str = "2026-05-20T12:00:00Z",
    cls: str = "heron",
    cam: str = "pond-north",
    etype: str = "detection",
    rid: str = "req-001",
) -> ActuationEvent:
    return ActuationEvent(
        timestamp=ts,
        trigger_class=cls,
        trigger_camera=cam,
        trigger_confidence=0.95,
        pre_delay_sec=1.0,
        total_duration_sec=5.0,
        actions=[
            DeviceAction(
                device_name="sprinkler-1",
                device_id="dev-001",
                device_type="sprinkler",
                duration_sec=3.0,
                delay_before_sec=0.5,
                success=True,
            ),
        ],
        request_id=rid,
        event_type=etype,
    )


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point actuation_db at a per-test temp database and reset module state."""
    import actuation_db

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    monkeypatch.setattr(actuation_db, "DB_PATH", db_path)
    # Reset thread-local connection so the new DB_PATH takes effect.
    if hasattr(_local, "conn"):
        del _local.conn
    # Reset the lazy-loader so each test can control the key.
    monkeypatch.setattr(actuation_db, "_hmac_key", None)
    monkeypatch.setattr(actuation_db, "_hmac_key_loaded", False)
    monkeypatch.setattr(actuation_db, "_hmac_key_warned", False)

    yield  # type: ignore[misc]

    # Cleanup
    if hasattr(_local, "conn"):
        _local.conn.close()
        del _local.conn
    os.unlink(db_path)


class TestSchemaColumns:
    """init_db must add prev_hash and row_hash columns."""

    def test_columns_exist_after_init(self) -> None:
        with patch.dict(os.environ, {"DETECTION_HMAC_KEY": _TEST_KEY_B64}):
            init_db()
        conn = _get_conn()
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(actuation_events)")}
        assert "prev_hash" in cols
        assert "row_hash" in cols

    def test_idempotent_migration(self) -> None:
        """Calling init_db twice must not fail."""
        with patch.dict(os.environ, {"DETECTION_HMAC_KEY": _TEST_KEY_B64}):
            init_db()
            init_db()  # no-op second call


class TestInsertWithHashChain:
    """insert_event must chain hashes when the HMAC key is present."""

    def test_single_event_hashes(self) -> None:
        with patch.dict(os.environ, {"DETECTION_HMAC_KEY": _TEST_KEY_B64}):
            init_db()
            eid = insert_event(_make_event())
        conn = _get_conn()
        row = conn.execute(
            "SELECT prev_hash, row_hash FROM actuation_events WHERE id = ?", (eid,),
        ).fetchone()
        assert row["prev_hash"] == ""  # first row — no predecessor
        assert len(row["row_hash"]) == 64  # SHA-256 hex digest

    def test_chain_links_sequential_events(self) -> None:
        with patch.dict(os.environ, {"DETECTION_HMAC_KEY": _TEST_KEY_B64}):
            init_db()
            insert_event(_make_event(ts="2026-05-20T12:00:00Z", rid="r1"))
            insert_event(_make_event(ts="2026-05-20T12:01:00Z", rid="r2"))
            insert_event(_make_event(ts="2026-05-20T12:02:00Z", rid="r3"))
        conn = _get_conn()
        rows = conn.execute(
            "SELECT id, prev_hash, row_hash FROM actuation_events ORDER BY id",
        ).fetchall()
        assert len(rows) == 3
        # First row's prev_hash is empty.
        assert rows[0]["prev_hash"] == ""
        # Each subsequent row's prev_hash equals the prior row's row_hash.
        assert rows[1]["prev_hash"] == rows[0]["row_hash"]
        assert rows[2]["prev_hash"] == rows[1]["row_hash"]

    def test_row_hash_is_deterministic(self) -> None:
        """Same inputs produce the same hash."""
        expected = _compute_row_hash(
            _TEST_KEY, "", "2026-05-20T12:00:00Z", "heron", "pond-north",
            "detection", "req-001",
        )
        with patch.dict(os.environ, {"DETECTION_HMAC_KEY": _TEST_KEY_B64}):
            init_db()
            insert_event(_make_event())
        conn = _get_conn()
        row = conn.execute(
            "SELECT row_hash FROM actuation_events ORDER BY id LIMIT 1",
        ).fetchone()
        assert row["row_hash"] == expected


class TestVerifyChain:
    """verify_chain must detect intact chains, tampering, and missing keys."""

    def test_empty_table_verifies(self) -> None:
        with patch.dict(os.environ, {"DETECTION_HMAC_KEY": _TEST_KEY_B64}):
            init_db()
            ok, msg = verify_chain()
        assert ok is True
        assert msg == "0 rows verified"

    def test_valid_chain_verifies(self) -> None:
        with patch.dict(os.environ, {"DETECTION_HMAC_KEY": _TEST_KEY_B64}):
            init_db()
            insert_event(_make_event(ts="2026-05-20T12:00:00Z", rid="r1"))
            insert_event(_make_event(ts="2026-05-20T12:01:00Z", rid="r2"))
            ok, msg = verify_chain()
        assert ok is True
        assert msg == "2 rows verified"

    def test_detects_tampered_row_hash(self) -> None:
        with patch.dict(os.environ, {"DETECTION_HMAC_KEY": _TEST_KEY_B64}):
            init_db()
            eid = insert_event(_make_event())
        # Tamper with the stored row_hash.
        conn = _get_conn()
        conn.execute(
            "UPDATE actuation_events SET row_hash = 'bad' WHERE id = ?", (eid,),
        )
        conn.commit()
        with patch.dict(os.environ, {"DETECTION_HMAC_KEY": _TEST_KEY_B64}):
            ok, msg = verify_chain()
        assert ok is False
        assert "row_hash mismatch" in msg

    def test_detects_tampered_prev_hash(self) -> None:
        with patch.dict(os.environ, {"DETECTION_HMAC_KEY": _TEST_KEY_B64}):
            init_db()
            insert_event(_make_event(ts="2026-05-20T12:00:00Z", rid="r1"))
            insert_event(_make_event(ts="2026-05-20T12:01:00Z", rid="r2"))
        conn = _get_conn()
        # Corrupt the second row's prev_hash.
        conn.execute(
            "UPDATE actuation_events SET prev_hash = 'bad' WHERE id = 2",
        )
        conn.commit()

        import actuation_db
        actuation_db._hmac_key_loaded = False
        actuation_db._hmac_key = None

        with patch.dict(os.environ, {"DETECTION_HMAC_KEY": _TEST_KEY_B64}):
            ok, msg = verify_chain()
        assert ok is False
        assert "prev_hash mismatch" in msg

    def test_detects_tampered_payload_field(self) -> None:
        with patch.dict(os.environ, {"DETECTION_HMAC_KEY": _TEST_KEY_B64}):
            init_db()
            insert_event(_make_event(cls="heron"))
        # Change a covered field.
        conn = _get_conn()
        conn.execute(
            "UPDATE actuation_events SET trigger_class = 'duck' WHERE id = 1",
        )
        conn.commit()
        with patch.dict(os.environ, {"DETECTION_HMAC_KEY": _TEST_KEY_B64}):
            ok, msg = verify_chain()
        assert ok is False
        assert "row_hash mismatch" in msg

    def test_verify_without_key(self) -> None:
        """verify_chain returns a clear error when the key is absent."""
        import actuation_db
        actuation_db._hmac_key_loaded = False
        actuation_db._hmac_key = None
        with patch.dict(os.environ, {"DETECTION_HMAC_KEY": ""}, clear=False):
            init_db()
            ok, msg = verify_chain()
        assert ok is False
        assert msg == "HMAC key not available"


class TestGracefulDegradation:
    """When DETECTION_HMAC_KEY is absent, events insert without hashes."""

    def test_insert_without_key_stores_empty_hashes(self) -> None:
        with patch.dict(os.environ, {"DETECTION_HMAC_KEY": ""}, clear=False):
            init_db()
            eid = insert_event(_make_event())
        conn = _get_conn()
        row = conn.execute(
            "SELECT prev_hash, row_hash FROM actuation_events WHERE id = ?", (eid,),
        ).fetchone()
        assert row["prev_hash"] == ""
        assert row["row_hash"] == ""

    def test_warning_logged_once(self, caplog: pytest.LogCaptureFixture) -> None:
        import actuation_db
        actuation_db._hmac_key_loaded = False
        actuation_db._hmac_key = None

        with patch.dict(os.environ, {"DETECTION_HMAC_KEY": ""}, clear=False):
            init_db()
            insert_event(_make_event(rid="r1"))
            insert_event(_make_event(rid="r2"))
        warnings = [r for r in caplog.records if "hash chain disabled" in r.message]
        assert len(warnings) == 1
