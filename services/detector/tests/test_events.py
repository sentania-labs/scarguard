import sqlite3
from datetime import datetime, timezone

from detector import Detection
from events import EventProcessor


def _count_rows(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM detection_events").fetchone()[0]


def test_persist_multiple_sequential_inserts(tmp_path):
    db_path = tmp_path / "events.db"
    processor = EventProcessor(
        cooldown_seconds=30,
        snapshot_dir=str(tmp_path / "snapshots"),
        db_path=str(db_path),
    )
    det = Detection(class_name="heron", confidence=0.9, bbox=(1, 2, 3, 4))

    processor._persist(datetime.now(timezone.utc), det, "cam-a", None)
    processor._persist(datetime.now(timezone.utc), det, "cam-a", None)
    processor._persist(datetime.now(timezone.utc), det, "cam-b", None)
    processor.close()

    assert _count_rows(str(db_path)) == 3


def test_persist_recovers_after_write_exception(monkeypatch, tmp_path):
    db_path = tmp_path / "events.db"
    processor = EventProcessor(
        cooldown_seconds=30,
        snapshot_dir=str(tmp_path / "snapshots"),
        db_path=str(db_path),
    )
    det = Detection(class_name="heron", confidence=0.9, bbox=(1, 2, 3, 4))

    original_insert = processor._insert_event
    state = {"fail_once": True}

    def flaky_insert(timestamp, det_arg, camera_name, snapshot_path):
        if state["fail_once"]:
            state["fail_once"] = False
            raise sqlite3.OperationalError("simulated insert failure")
        return original_insert(timestamp, det_arg, camera_name, snapshot_path)

    monkeypatch.setattr(processor, "_insert_event", flaky_insert)

    processor._persist(datetime.now(timezone.utc), det, "cam-a", None)
    processor._persist(datetime.now(timezone.utc), det, "cam-a", None)
    processor.close()

    # First insert fails and triggers a connection reset; second insert succeeds.
    assert _count_rows(str(db_path)) == 1


def test_persist_swallows_reset_connection_errors(monkeypatch, tmp_path):
    db_path = tmp_path / "events.db"
    processor = EventProcessor(
        cooldown_seconds=30,
        snapshot_dir=str(tmp_path / "snapshots"),
        db_path=str(db_path),
    )
    det = Detection(class_name="heron", confidence=0.9, bbox=(1, 2, 3, 4))

    def always_fail_insert(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated insert failure")

    def fail_reset():
        raise sqlite3.OperationalError("simulated reset failure")

    monkeypatch.setattr(processor, "_insert_event", always_fail_insert)
    monkeypatch.setattr(processor, "_reset_connection_locked", fail_reset)

    # _persist should never raise, even if recovery fails.
    processor._persist(datetime.now(timezone.utc), det, "cam-a", None)
    processor.close()
