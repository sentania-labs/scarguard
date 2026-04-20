import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

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

    processor._persist(datetime.now(timezone.utc), det, "cam-a", None, None)
    processor._persist(datetime.now(timezone.utc), det, "cam-a", None, None)
    processor._persist(datetime.now(timezone.utc), det, "cam-b", None, None)
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

    def flaky_insert(timestamp, det_arg, camera_name, snapshot_path, actions_triggered, frame_size=None):
        if state["fail_once"]:
            state["fail_once"] = False
            raise sqlite3.OperationalError("simulated insert failure")
        return original_insert(timestamp, det_arg, camera_name, snapshot_path, actions_triggered, frame_size)

    monkeypatch.setattr(processor, "_insert_event", flaky_insert)

    processor._persist(datetime.now(timezone.utc), det, "cam-a", None, None)
    processor._persist(datetime.now(timezone.utc), det, "cam-a", None, None)
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
    processor._persist(datetime.now(timezone.utc), det, "cam-a", None, None)
    processor.close()


# ------------------------------------------------------------------
# Action rule filtering tests
# ------------------------------------------------------------------

def _make_processor(tmp_path: Path) -> EventProcessor:
    return EventProcessor(
        cooldown_seconds=0,
        snapshot_dir=str(tmp_path / "snapshots"),
        db_path=str(tmp_path / "events.db"),
    )


def _dummy_frame() -> np.ndarray:
    return np.zeros((100, 100, 3), dtype=np.uint8)


def test_process_no_rules_notifies_all(tmp_path):
    """actions_by_class=None (no rules) → events published with actions_triggered=[]."""
    processor = _make_processor(tmp_path)
    det = Detection(class_name="heron", confidence=0.9, bbox=(10, 10, 50, 50))
    events = processor.process([det], "cam-a", _dummy_frame(), actions_by_class=None)
    assert len(events) == 1
    assert events[0]["actions_triggered"] == []
    processor.close()


def test_process_matching_rule_notifies_named_channels(tmp_path):
    """Matching action rule → events published with specific channel names."""
    processor = _make_processor(tmp_path)
    det = Detection(class_name="bird", confidence=0.9, bbox=(10, 10, 50, 50))
    actions_by_class = {"bird": ["bird-alerts-email"]}
    events = processor.process([det], "cam-a", _dummy_frame(), actions_by_class=actions_by_class)
    assert len(events) == 1
    assert events[0]["actions_triggered"] == ["bird-alerts-email"]
    processor.close()


def test_process_non_matching_rule_suppresses_event(tmp_path):
    """Non-matching class with action rules → persisted but not published."""
    processor = _make_processor(tmp_path)
    det = Detection(class_name="bench", confidence=0.5, bbox=(10, 10, 50, 50))
    # Rules only match "bird"; "bench" should be suppressed.
    actions_by_class = {"bench": None}
    events = processor.process([det], "cam-a", _dummy_frame(), actions_by_class=actions_by_class)
    assert len(events) == 0
    # But the detection should still be persisted to DB.
    assert _count_rows(str(tmp_path / "events.db")) == 1
    processor.close()


def _match_notification_rules(class_name: str, rules: list[dict]) -> list[str] | None:
    """Local copy of detector _match_notification_rules for testing without cv2."""
    for rule in rules:
        rule_class = rule.get("class_name", "*")
        if rule_class == "*" or rule_class == class_name:
            return list(rule.get("channels", []))
    return None


def _match_deterrent_rules(class_name: str, rules: list[dict]) -> list[str]:
    """Local copy of detector _match_deterrent_rules for testing without cv2."""
    for rule in rules:
        rule_class = rule.get("class_name", "*")
        if rule_class == "*" or rule_class == class_name:
            return list(rule.get("groups", []))
    return []


def test_match_notification_rules_returns_none_for_no_match():
    """_match_notification_rules returns None when no rule matches the class."""
    rules = [{"class_name": "bird", "channels": ["bird-alerts"]}]
    assert _match_notification_rules("bench", rules) is None


def test_match_notification_rules_returns_channels_for_match():
    """_match_notification_rules returns channel list for matching rule."""
    rules = [{"class_name": "bird", "channels": ["bird-alerts-email", "bird-alerts-discord"]}]
    assert _match_notification_rules("bird", rules) == ["bird-alerts-email", "bird-alerts-discord"]


def test_match_notification_rules_wildcard_matches_any():
    """Wildcard rule matches any class."""
    rules = [{"class_name": "*", "channels": ["all-alerts"]}]
    assert _match_notification_rules("anything", rules) == ["all-alerts"]


def test_match_deterrent_rules_returns_empty_for_no_match():
    """v0.13.3: deterrents default to do-nothing when no rule matches
    (unlike notifications, which default to notify-all)."""
    rules = [{"class_name": "bird", "groups": ["thermonuclear"]}]
    assert _match_deterrent_rules("bench", rules) == []


def test_match_deterrent_rules_returns_groups_for_match():
    rules = [{"class_name": "heron", "groups": ["thermonuclear", "siren"]}]
    assert _match_deterrent_rules("heron", rules) == ["thermonuclear", "siren"]


def test_match_deterrent_rules_wildcard_matches_any():
    rules = [{"class_name": "*", "groups": ["minor"]}]
    assert _match_deterrent_rules("anything", rules) == ["minor"]


def test_match_deterrent_rules_first_match_wins():
    rules = [
        {"class_name": "heron", "groups": ["thermonuclear"]},
        {"class_name": "*", "groups": ["minor"]},
    ]
    assert _match_deterrent_rules("heron", rules) == ["thermonuclear"]
    assert _match_deterrent_rules("duck", rules) == ["minor"]
