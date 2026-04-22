"""Tests for the actuation-duration safety helpers in shared/deterrent_safety.py."""

from __future__ import annotations

import math

import pytest
from deterrent_safety import (
    DEFAULT_TEST_FIRE_SEC,
    MAX_ACTUATION_SEC,
    MAX_TEST_FIRE_SEC,
    MIN_ACTUATION_SEC,
    clamp_duration,
)


class TestClampDuration:
    def test_passes_through_valid_value(self) -> None:
        assert clamp_duration(5.0, max_sec=15.0, default=3.0) == 5.0

    def test_clamps_above_max(self) -> None:
        # Even comically large values must be bounded — this is the core
        # physical-safety contract.
        assert clamp_duration(86400.0, max_sec=15.0, default=3.0) == 15.0

    def test_clamps_below_min(self) -> None:
        assert clamp_duration(0.001, max_sec=15.0, default=3.0) == MIN_ACTUATION_SEC

    def test_negative_clamped_to_min(self) -> None:
        assert clamp_duration(-100, max_sec=15.0, default=3.0) == MIN_ACTUATION_SEC

    def test_nan_falls_back_to_default(self) -> None:
        out = clamp_duration(float("nan"), max_sec=15.0, default=3.0)
        assert out == 3.0
        assert not math.isnan(out)

    def test_inf_falls_back_to_default(self) -> None:
        assert clamp_duration(float("inf"), max_sec=15.0, default=3.0) == 3.0
        assert clamp_duration(float("-inf"), max_sec=15.0, default=3.0) == 3.0

    def test_string_falls_back_to_default(self) -> None:
        assert clamp_duration("not a number", max_sec=15.0, default=3.0) == 3.0

    def test_none_falls_back_to_default(self) -> None:
        assert clamp_duration(None, max_sec=15.0, default=3.0) == 3.0

    def test_dict_falls_back_to_default(self) -> None:
        assert clamp_duration({"haha": 1}, max_sec=15.0, default=3.0) == 3.0

    def test_default_itself_is_clamped(self) -> None:
        # If someone passes a default above max, it still gets clamped.
        assert clamp_duration(None, max_sec=15.0, default=999.0) == 15.0

    def test_uses_max_actuation_sec_constant(self) -> None:
        assert MAX_ACTUATION_SEC >= MAX_TEST_FIRE_SEC
        assert MAX_TEST_FIRE_SEC > MIN_ACTUATION_SEC > 0
        assert DEFAULT_TEST_FIRE_SEC >= MIN_ACTUATION_SEC
        assert DEFAULT_TEST_FIRE_SEC <= MAX_TEST_FIRE_SEC


class TestSafetyConstants:
    """Sanity bounds — if these fail, someone tightened the caps without
    updating the test, which is fine; if they relaxed them dangerously,
    this catches it."""

    def test_max_test_fire_is_conservative(self) -> None:
        # 15s is the documented backyard pond ceiling. Don't loosen
        # without re-thinking the threat model.
        assert MAX_TEST_FIRE_SEC <= 30.0

    def test_max_actuation_is_conservative(self) -> None:
        # 60s outer envelope. Anything above this and an attacker can drain
        # a typical garden hose before the watchdog catches it.
        assert MAX_ACTUATION_SEC <= 120.0

    def test_min_is_meaningful(self) -> None:
        # Below ~0.5s most Tuya devices barely register the ON command.
        assert MIN_ACTUATION_SEC >= 0.1


@pytest.mark.parametrize("hostile", [
    1e9,         # one billion seconds (~31 years)
    86400.0,     # one day
    3600.0,      # one hour
    61.0,        # 61 seconds — one second past MAX_ACTUATION_SEC
])
def test_hostile_inputs_are_bounded_to_max_actuation(hostile: float) -> None:
    """Whatever the layer, the absolute ceiling is MAX_ACTUATION_SEC."""
    out = clamp_duration(hostile, max_sec=MAX_ACTUATION_SEC, default=3.0)
    assert out <= MAX_ACTUATION_SEC
    assert out >= MIN_ACTUATION_SEC
