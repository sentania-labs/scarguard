"""Tests for the cooldown tracker."""

from __future__ import annotations

import time
from unittest.mock import patch

from cooldown import CooldownTracker, GroupCooldownTracker


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


class TestFreshBootSentinel:
    """A never-fired tracker must read clear even on a just-booted host.

    ``time.monotonic()`` counts from system boot, so these assertions only
    failed on a machine whose uptime was below the cooldown.  That is rare on
    a workstation and normal on an ephemeral CI runner, which is where the
    bug surfaced.  Patching monotonic to a small value reproduces it on any
    host, so this stays a real regression test rather than a coincidence.
    """

    def test_global_clear_when_uptime_below_cooldown(self) -> None:
        cd = CooldownTracker()
        with patch("cooldown.time.monotonic", return_value=5.0):
            assert cd.is_clear(600) is True
            assert cd.seconds_remaining(600) == 0.0

    def test_group_clear_when_uptime_below_cooldown(self) -> None:
        gcd = GroupCooldownTracker()
        with patch("cooldown.time.monotonic", return_value=5.0):
            assert gcd.is_clear("thermonuclear", 600) is True
            assert gcd.seconds_remaining("thermonuclear", 600) == 0.0

    def test_recording_still_gates_on_a_fresh_host(self) -> None:
        """The fix must not turn the gate off, only fix the never-fired case."""
        cd = CooldownTracker()
        gcd = GroupCooldownTracker()
        with patch("cooldown.time.monotonic", return_value=5.0):
            cd.record()
            gcd.record("thermonuclear")
            assert cd.is_clear(600) is False
            assert gcd.is_clear("thermonuclear", 600) is False
