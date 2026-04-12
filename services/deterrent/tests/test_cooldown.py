"""Tests for the cooldown tracker."""

from __future__ import annotations

import time
from unittest.mock import patch

from cooldown import CooldownTracker


class TestCooldownTracker:
    def test_initially_clear(self) -> None:
        cd = CooldownTracker()
        assert cd.is_clear(60) is True

    def test_not_clear_after_record(self) -> None:
        cd = CooldownTracker()
        cd.record()
        assert cd.is_clear(60) is False

    def test_clear_after_cooldown_expires(self) -> None:
        cd = CooldownTracker()
        cd.record()
        # Simulate time passing by patching monotonic
        recorded_time = time.monotonic()
        with patch("cooldown.time.monotonic", return_value=recorded_time + 61):
            assert cd.is_clear(60) is True

    def test_seconds_remaining(self) -> None:
        cd = CooldownTracker()
        cd.record()
        remaining = cd.seconds_remaining(60)
        assert 59.0 <= remaining <= 60.0

    def test_seconds_remaining_zero_when_clear(self) -> None:
        cd = CooldownTracker()
        assert cd.seconds_remaining(60) == 0.0

    def test_zero_cooldown_always_clear(self) -> None:
        cd = CooldownTracker()
        cd.record()
        assert cd.is_clear(0) is True
