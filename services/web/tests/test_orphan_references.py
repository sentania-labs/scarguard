"""Tests for _find_orphan_references — the soft-warn on dangling rule refs."""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


from routes.config import _find_orphan_references  # noqa: E402


def test_no_warnings_for_clean_config():
    cfg = {
        "notifications": {
            "channels": [{"name": "pond-alerts", "type": "discord"}],
        },
        "deterrent": {
            "groups": [{"name": "minor", "devices": []}],
        },
        "cameras": [
            {
                "name": "pond-north",
                "notification_rules": [{"class_name": "*", "channels": ["pond-alerts"]}],
                "deterrent_rules": [{"class_name": "heron", "groups": ["minor"]}],
            }
        ],
        "system": {"summary_report": {"channels": ["pond-alerts"]}},
    }
    assert _find_orphan_references(cfg) == []


def test_warns_on_missing_notification_channel():
    cfg = {
        "notifications": {"channels": [{"name": "pond-alerts", "type": "discord"}]},
        "cameras": [
            {
                "name": "pond-north",
                "notification_rules": [
                    {"class_name": "*", "channels": ["pond-alerts", "ghost-channel"]},
                ],
            }
        ],
    }
    warnings = _find_orphan_references(cfg)
    assert len(warnings) == 1
    assert "pond-north" in warnings[0]
    assert "ghost-channel" in warnings[0]


def test_warns_on_missing_deterrent_group():
    cfg = {
        "deterrent": {"groups": [{"name": "minor", "devices": []}]},
        "cameras": [
            {
                "name": "pond-south",
                "deterrent_rules": [
                    {"class_name": "*", "groups": ["thermonuclear"]},
                ],
            }
        ],
    }
    warnings = _find_orphan_references(cfg)
    assert len(warnings) == 1
    assert "pond-south" in warnings[0]
    assert "thermonuclear" in warnings[0]


def test_warns_on_summary_report_orphan():
    cfg = {
        "notifications": {"channels": [{"name": "pond-alerts", "type": "discord"}]},
        "system": {"summary_report": {"channels": ["nope"]}},
    }
    warnings = _find_orphan_references(cfg)
    assert len(warnings) == 1
    assert "nope" in warnings[0]
    assert "summary report" in warnings[0].lower()


def test_multiple_warnings_aggregate():
    cfg = {
        "notifications": {"channels": [{"name": "a", "type": "discord"}]},
        "deterrent": {"groups": [{"name": "g1", "devices": []}]},
        "cameras": [
            {
                "name": "cam1",
                "notification_rules": [{"class_name": "*", "channels": ["zz"]}],
                "deterrent_rules": [{"class_name": "*", "groups": ["yy"]}],
            }
        ],
        "system": {"summary_report": {"channels": ["xx"]}},
    }
    warnings = _find_orphan_references(cfg)
    assert len(warnings) == 3


def test_handles_missing_sections_gracefully():
    # Config with no cameras / no notifications / no deterrent
    assert _find_orphan_references({}) == []
    assert _find_orphan_references({"cameras": []}) == []


def test_handles_malformed_entries():
    # Malformed (non-dict) entries should be skipped, not raise.
    cfg = {
        "notifications": {"channels": [None, "not-a-dict", {"name": "ok"}]},
        "cameras": [
            "not-a-dict",
            {"name": "cam", "notification_rules": [None, {"channels": ["ok"]}]},
        ],
    }
    # Should not raise, should find no orphans since "ok" resolves
    assert _find_orphan_references(cfg) == []
