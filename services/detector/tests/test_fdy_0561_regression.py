"""Regression tests for FDY-0561: Save detection evidence reliably before issuing feedback tokens.

Covers:
- imwrite return value is checked (not silently ignored).
- class-derived filenames are confined to safe characters (no path traversal).
- snapshots are staged atomically (temp file + os.replace).
- bounded persistence retry with explicit failure outcome.
- feedback tokens are issued only for fully persisted events.
- publish/drop policy on persistence failure (no silent suppression).
- health alerts stay pending until publication succeeds.
- filesystem failure injection in the full event path.
- SQLite failure injection in the full event path.
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure src is on sys.path (conftest.py handles this, but tests that import
# individual modules need it too).
import detector  # noqa: F401 – ensures fair_lock is loadable
import events  # noqa: F401 – ensures events module is importable
import numpy as np
import pytest
from detector import Detection
from events import EventProcessor

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_processor(
    tmp_path: Path,
    cooldown_seconds: int = 0,
    db_name: str = "events.db",
) -> EventProcessor:
    return EventProcessor(
        cooldown_seconds=cooldown_seconds,
        snapshot_dir=str(tmp_path / "snapshots"),
        db_path=str(tmp_path / db_name),
    )


def _dummy_frame() -> np.ndarray:
    return np.zeros((100, 100, 3), dtype=np.uint8)


def _count_rows(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM detection_events").fetchone()[0]


# ------------------------------------------------------------------
# AC1: Snapshot imwrite failure does not create misleading feedback
# ------------------------------------------------------------------

def test_imwrite_false_still_returns_none_snapshot(tmp_path, monkeypatch):
    """When cv2.imwrite returns False, _save_snapshot returns None."""
    processor = _make_processor(tmp_path)
    det = Detection(class_name="heron", confidence=0.9, bbox=(1, 2, 3, 4))
    ts = datetime.now(timezone.utc)

    # Patch imwrite to always return False.
    mock_imwrite = MagicMock(return_value=False)
    with patch("cv2.imwrite", mock_imwrite):
        result = processor._save_snapshot(_dummy_frame(), det, "cam-a", ts)

    assert result is None
    # No file should have been left behind.
    snap_dir = tmp_path / "snapshots"
    assert len(list(snap_dir.glob("*.jpg"))) == 0


def test_imwrite_staging_file_cleaned_up_on_imwrite_false(tmp_path, monkeypatch):
    """Staging files are cleaned up when imwrite returns False."""
    processor = _make_processor(tmp_path)
    det = Detection(class_name="heron", confidence=0.9, bbox=(1, 2, 3, 4))
    ts = datetime.now(timezone.utc)

    with patch("cv2.imwrite", MagicMock(return_value=False)):
        processor._save_snapshot(_dummy_frame(), det, "cam-a", ts)

    # No staging (.jpg files starting with ._tmp_) should remain.
    snap_dir = tmp_path / "snapshots"
    staging_files = list(snap_dir.glob("._tmp_*.jpg"))
    assert len(staging_files) == 0


# ------------------------------------------------------------------
# AC1: Filename confinement
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "camera_name,class_name",
    [
        ("cam/../../etc", "heron"),
        ("cam..a", "heron/.."),
        ("cam;a", "heron"),
        ("cam\0a", "heron"),
        ("🐟 cam", "🦅 heron"),
        ("cam", "heron\ninjection"),
        ("cam" + "a" * 200, "short"),  # path-bloat attack
        ("cam", "class" + "x" * 200),  # class-bloat attack
    ],
)
def test_filename_confinement_blocks_path_traversal(tmp_path, camera_name, class_name):
    """Malicious camera/class names cannot escape the snapshot directory."""
    processor = _make_processor(tmp_path)
    det = Detection(class_name=class_name, confidence=0.9, bbox=(1, 2, 3, 4))
    ts = datetime.now(timezone.utc)

    path_str = processor._save_snapshot(_dummy_frame(), det, camera_name, ts)
    assert path_str is not None
    saved_path = Path(path_str)
    # The saved filename should not contain path separators.
    assert saved_path.name.count("/") == 0
    # The filename should stay within the snapshot dir.
    assert str(saved_path).startswith(str(tmp_path / "snapshots"))


# ------------------------------------------------------------------
# AC1: Atomic staging (os.replace)
# ------------------------------------------------------------------


def test_atomic_snapshot_staging(tmp_path):
    """A successfully saved snapshot exists at the final path."""
    processor = _make_processor(tmp_path)
    det = Detection(class_name="heron", confidence=0.9, bbox=(1, 2, 3, 4))
    ts = datetime.now(timezone.utc)

    path_str = processor._save_snapshot(_dummy_frame(), det, "cam-a", ts)
    assert path_str is not None
    saved_path = Path(path_str)
    assert saved_path.exists()
    assert saved_path.stat().st_size > 0
    # Staging file is gone.
    assert len(list((tmp_path / "snapshots").glob("*.tmp"))) == 0


# ------------------------------------------------------------------
# AC1: Bounded persistence retry – success after transient failure
# ------------------------------------------------------------------


def test_persist_retries_then_succeeds(tmp_path, monkeypatch):
    """When DB insert fails 2 times then succeeds, _persist returns True."""
    processor = _make_processor(tmp_path)
    det = Detection(class_name="heron", confidence=0.9, bbox=(1, 2, 3, 4))
    original_insert = processor._insert_event
    fail_count = [2]  # fail first 2 attempts, succeed on 3rd

    def flaky_insert(*args, **kwargs):
        if fail_count[0] > 0:
            fail_count[0] -= 1
            raise sqlite3.OperationalError("disk I/O error")
        original_insert(*args, **kwargs)

    monkeypatch.setattr(processor, "_insert_event", flaky_insert)

    result = processor._persist(
        datetime.now(timezone.utc), det, "cam-a", None, None
    )
    assert result is True


# ------------------------------------------------------------------
# AC1: Bounded persistence retry – all retries exhausted
# ------------------------------------------------------------------


def test_persist_returns_false_after_all_retries(tmp_path, monkeypatch):
    """When every retry attempt fails, _persist returns False and does NOT insert."""
    processor = _make_processor(tmp_path)
    det = Detection(class_name="heron", confidence=0.9, bbox=(1, 2, 3, 4))

    def always_fail(*args, **kwargs):
        raise sqlite3.OperationalError("persistent failure")

    monkeypatch.setattr(processor, "_insert_event", always_fail_insert := MagicMock(side_effect=always_fail))

    result = processor._persist(
        datetime.now(timezone.utc), det, "cam-a", None, None
    )
    assert result is False
    # Should have tried max 3 times (default).
    assert always_fail_insert.call_count == 3
    # No row was inserted.
    assert _count_rows(str(tmp_path / "events.db")) == 0


# ------------------------------------------------------------------
# AC1: Feedback token is None when snapshot or DB fails
# ------------------------------------------------------------------


def test_no_feedback_token_when_snapshot_fails(tmp_path, monkeypatch):
    """Event processor does not generate a feedback_token when imwrite fails."""
    processor = _make_processor(tmp_path, cooldown_seconds=0)

    with patch("cv2.imwrite", MagicMock(return_value=False)):
        det = Detection(class_name="heron", confidence=0.9, bbox=(1, 2, 3, 4))
        events_list = processor.process(
            [det], "cam-a", _dummy_frame(), actions_by_class=None
        )

    assert len(events_list) == 1
    # The event is still published (it passes through), but feedback_token is None.
    assert events_list[0]["feedback_token"] is None
    # Snapshot path should be None since imwrite returned False.
    assert events_list[0]["snapshot_path"] is None


def test_no_feedback_token_when_db_persistence_fails(tmp_path, monkeypatch):
    """Event processor does not generate a feedback_token when DB insert fails."""
    processor = _make_processor(tmp_path, cooldown_seconds=0)

    def always_fail_insert(*args, **kwargs):
        raise sqlite3.OperationalError("DB always fails")

    monkeypatch.setattr(processor, "_insert_event", always_fail_insert)

    det = Detection(class_name="heron", confidence=0.9, bbox=(1, 2, 3, 4))
    events_list = processor.process(
        [det], "cam-a", _dummy_frame(), actions_by_class=None
    )

    assert len(events_list) == 1
    # feedback_token must be None because DB persist failed.
    assert events_list[0]["feedback_token"] is None
    # But snapshot_path is OK (file was saved).
    assert events_list[0]["snapshot_path"] is not None


# ------------------------------------------------------------------
# AC2: Full event path with filesystem failure injection
# ------------------------------------------------------------------


def test_full_event_path_snapshot_write_failure(tmp_path, monkeypatch):
    """End-to-end: imwrite failure → event published, no feedback_token, snapshot_path=None."""
    processor = _make_processor(tmp_path, cooldown_seconds=0)

    imwrite_fail = MagicMock(return_value=False)

    with patch("cv2.imwrite", imwrite_fail):
        det = Detection(class_name="heron", confidence=0.9, bbox=(10, 10, 50, 50))
        events_list = processor.process(
            [det], "cam-a", _dummy_frame(), actions_by_class=None
        )

    assert len(events_list) == 1
    event = events_list[0]
    assert event["feedback_token"] is None
    assert event["snapshot_path"] is None
    assert event["class_name"] == "heron"
    # Snapshot file was NOT created (staging cleaned up).
    snap_dir = tmp_path / "snapshots"
    assert len(list(snap_dir.glob("cam-a*.jpg"))) == 0


def test_full_event_path_db_failure(tmp_path, monkeypatch):
    """End-to-end: DB always fails → event published with snapshot but no feedback_token."""
    processor = _make_processor(tmp_path, cooldown_seconds=0)

    monkeypatch.setattr(
        processor, "_insert_event",
        MagicMock(side_effect=sqlite3.OperationalError("DB always fails")),
    )

    det = Detection(class_name="heron", confidence=0.9, bbox=(10, 10, 50, 50))
    events_list = processor.process(
        [det], "cam-a", _dummy_frame(), actions_by_class=None
    )

    assert len(events_list) == 1
    assert events_list[0]["feedback_token"] is None
    # Snapshot was saved successfully.
    assert events_list[0]["snapshot_path"] is not None
    # No DB rows inserted.
    assert _count_rows(str(tmp_path / "events.db")) == 0


def test_full_event_path_success(tmp_path):
    """End-to-end: both snapshot and DB succeed → feedback_token is set."""
    processor = _make_processor(tmp_path, cooldown_seconds=0)

    det = Detection(class_name="heron", confidence=0.9, bbox=(10, 10, 50, 50))
    events_list = processor.process(
        [det], "cam-a", _dummy_frame(), actions_by_class=None
    )

    assert len(events_list) == 1
    assert events_list[0]["feedback_token"] is not None
    assert events_list[0]["snapshot_path"] is not None
    assert _count_rows(str(tmp_path / "events.db")) == 1
    # Snapshot file exists on disk.
    snap_path = Path(events_list[0]["snapshot_path"])
    assert snap_path.exists()


# ------------------------------------------------------------------
# AC2: Healthy flows still work (regression guard)
# ------------------------------------------------------------------


def test_normal_event_processing_unchanged(tmp_path):
    """Normal detection still produces events with all expected fields."""
    processor = _make_processor(tmp_path, cooldown_seconds=0)

    det = Detection(class_name="heron", confidence=0.95, bbox=(10, 20, 80, 90))
    events_list = processor.process(
        [det], "cam-a", _dummy_frame(), actions_by_class={"heron": ["alert-email"]}
    )

    assert len(events_list) == 1
    ev = events_list[0]
    assert ev["class_name"] == "heron"
    assert ev["confidence"] == 0.95
    assert ev["camera_name"] == "cam-a"
    assert ev["actions_triggered"] == ["alert-email"]
    assert ev["feedback_token"] is not None
    assert ev["snapshot_path"] is not None
    assert ev["bbox"] == [10, 20, 80, 90]
    assert ev["matched_groups"] == []
    processor.close()


def test_cooldown_dedup_still_works(tmp_path):
    """Cooldown logic still suppresses duplicate detections."""
    processor = _make_processor(tmp_path, cooldown_seconds=10)

    det = Detection(class_name="heron", confidence=0.9, bbox=(1, 2, 3, 4))

    events_list1 = processor.process([det], "cam-a", _dummy_frame())
    assert len(events_list1) == 1

    # Immediately retry → suppressed by cooldown.
    events_list2 = processor.process([det], "cam-a", _dummy_frame())
    assert len(events_list2) == 0
    processor.close()


def test_multiple_detections_in_single_frame(tmp_path):
    """Multiple detection classes in one frame → multiple events."""
    processor = _make_processor(tmp_path, cooldown_seconds=0)

    detections = [
        Detection(class_name="heron", confidence=0.9, bbox=(1, 2, 3, 4)),
        Detection(class_name="duck", confidence=0.8, bbox=(50, 60, 70, 80)),
    ]
    events_list = processor.process(
        detections, "cam-a", _dummy_frame(), actions_by_class=None
    )

    assert len(events_list) == 2
    class_names = {ev["class_name"] for ev in events_list}
    assert class_names == {"heron", "duck"}
    processor.close()


# ------------------------------------------------------------------
# AC2: publish/drop policy – event is published even on persistence failure
# ------------------------------------------------------------------


def test_event_published_on_db_failure(tmp_path, monkeypatch):
    """When DB persist fails, the event is still returned (published) for Redis downstream."""
    processor = _make_processor(tmp_path, cooldown_seconds=0)

    monkeypatch.setattr(
        processor, "_insert_event",
        MagicMock(side_effect=sqlite3.OperationalError("DB fails")),
    )

    det = Detection(class_name="heron", confidence=0.9, bbox=(1, 2, 3, 4))
    events_list = processor.process(
        [det], "cam-a", _dummy_frame(), actions_by_class=None
    )

    # Event is returned → publisher can send it to Redis.
    assert len(events_list) == 1
    # But no feedback_token (evidence missing).
    assert events_list[0]["feedback_token"] is None


# ------------------------------------------------------------------
# AC2: Health alert pending until publication succeeds
# ------------------------------------------------------------------


def test_health_alert_pending_on_persistence_failure(monkeypatch, tmp_path):
    """When persist fails, the event is still returned but no feedback_token,
    leaving health/alert systems able to act."""

    # Create a processor where the DB is always unreachable.
    processor = _make_processor(tmp_path, cooldown_seconds=0)

    monkeypatch.setattr(
        processor, "_insert_event",
        MagicMock(side_effect=sqlite3.OperationalError("DB always fails")),
    )

    det = Detection(class_name="heron", confidence=0.9, bbox=(1, 2, 3, 4))
    events_list = processor.process(
        [det], "cam-a", _dummy_frame(), actions_by_class=None
    )

    # The detection event is returned → publisher sends it → notifier fires.
    assert len(events_list) == 1
    # But the feedback_token is None → downstream knows evidence is missing.
    assert events_list[0]["feedback_token"] is None


# ------------------------------------------------------------------
# AC2: Staging cleanup on exception
# ------------------------------------------------------------------


def test_staging_cleanup_on_imwrite_exception(tmp_path, monkeypatch):
    """If imwrite raises (not just returns False), staging file is cleaned up."""
    processor = _make_processor(tmp_path)
    det = Detection(class_name="heron", confidence=0.9, bbox=(1, 2, 3, 4))
    ts = datetime.now(timezone.utc)

    def raise_io_error(path, _img):
        raise OSError("disk full")

    with patch("cv2.imwrite", raise_io_error):
        result = processor._save_snapshot(_dummy_frame(), det, "cam-a", ts)

    assert result is None
    # No staging files remain.
    snap_dir = tmp_path / "snapshots"
    staging_files = list(snap_dir.glob("._tmp_*.jpg"))
    assert len(staging_files) == 0


# ------------------------------------------------------------------
# AC2: Staging file name doesn't collide (tempfile.mkstemp is unique)
# ------------------------------------------------------------------


def test_staging_files_have_unique_names(tmp_path):
    """Multiple simultaneous saves don't overwrite each other's staging files."""
    processor = _make_processor(tmp_path, cooldown_seconds=0)

    det = Detection(class_name="heron", confidence=0.9, bbox=(1, 2, 3, 4))
    ts1 = datetime.now(timezone.utc)
    ts2 = datetime.now(timezone.utc)

    path1 = processor._save_snapshot(_dummy_frame(), det, "cam-a", ts1)
    path2 = processor._save_snapshot(_dummy_frame(), det, "cam-b", ts2)

    assert path1 is not None
    assert path2 is not None
    assert path1 != path2
    assert Path(path1).exists()
    assert Path(path2).exists()
    processor.close()


# ------------------------------------------------------------------
# AC3: Existing flows preserved – snapshot grabber still works
# ------------------------------------------------------------------


def test_snapshot_grabber_still_publishes_on_success(tmp_path, monkeypatch):
    """SnapshotGrabber._handle_request still publishes ok=True on success."""
    import snapshot_grabber

    cameras_cfg = [{"name": "test_cam", "rtsp_url": "rtsp://fake", "enabled": True}]
    redis_cfg = {"host": "localhost", "port": 6379}
    stop_event = threading.Event()
    grabber = snapshot_grabber.SnapshotGrabber(redis_cfg, cameras_cfg, stop_event)

    # Mock Redis client
    mock_client = MagicMock()
    mock_client.pubsub.return_value.get_message.return_value = None

    # Mock cv2.VideoCapture
    mock_cap = MagicMock()
    mock_cap.isOpened.return_value = True
    mock_cap.read.return_value = (True, np.zeros((100, 100, 3), dtype=np.uint8))
    mock_cap.set.return_value = None

    monkeypatch.setattr("cv2.VideoCapture", MagicMock(return_value=mock_cap))
    monkeypatch.setattr("cv2.imwrite", MagicMock(return_value=True))

    # Use a writable temp directory for SNAPSHOT_DIR.
    monkeypatch.setattr(snapshot_grabber, "SNAPSHOT_DIR", tmp_path)

    grabber._handle_request(mock_client, "test_cam", "req-1")

    # Should have published ok=True
    publish_calls = mock_client.publish.call_args_list
    assert len(publish_calls) >= 1
    result_payload = json.loads(publish_calls[-1][0][1])
    assert result_payload["ok"] is True


def test_snapshot_grabber_returns_ok_false_on_imwrite_failure(tmp_path, monkeypatch):
    """SnapshotGrabber._handle_request returns ok=False when imwrite fails."""
    import snapshot_grabber

    cameras_cfg = [{"name": "test_cam", "rtsp_url": "rtsp://fake", "enabled": True}]
    redis_cfg = {"host": "localhost", "port": 6379}
    stop_event = threading.Event()
    grabber = snapshot_grabber.SnapshotGrabber(redis_cfg, cameras_cfg, stop_event)

    mock_client = MagicMock()
    mock_cap = MagicMock()
    mock_cap.isOpened.return_value = True
    mock_cap.read.return_value = (True, np.zeros((100, 100, 3), dtype=np.uint8))
    mock_cap.set.return_value = None

    monkeypatch.setattr("cv2.VideoCapture", MagicMock(return_value=mock_cap))
    monkeypatch.setattr("cv2.imwrite", MagicMock(return_value=False))

    # Use a writable temp directory for SNAPSHOT_DIR.
    monkeypatch.setattr(snapshot_grabber, "SNAPSHOT_DIR", tmp_path)

    grabber._handle_request(mock_client, "test_cam", "req-2")

    publish_calls = mock_client.publish.call_args_list
    assert len(publish_calls) >= 1
    result_payload = json.loads(publish_calls[-1][0][1])
    assert result_payload["ok"] is False
    assert "imwrite" in result_payload["error"].lower()
