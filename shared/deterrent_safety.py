"""Deterrent actuation safety bounds.

Hard limits on how long a physical deterrent device (sprinkler, siren,
light) can be held ON by any code path. Enforced at every layer that
accepts a duration: web API, deterrent request handler, randomisation
plan for detection-driven firing, and the cloud controller's own
watchdog. Multiple layers on purpose - if one gets bypassed, the others
still bound the physical effect.

Do not tighten or loosen these without thinking about the pond. The
caps are conservative for a backyard koi pond: 15s is long enough to
visibly startle wildlife, 60s is an outer envelope that catches any
misconfiguration without draining the water supply.
"""

from __future__ import annotations

import math
from typing import Any

MIN_ACTUATION_SEC: float = 0.5
MAX_TEST_FIRE_SEC: float = 15.0
MAX_ACTUATION_SEC: float = 60.0
# Ceiling on the firing window of an admin group test-fire. Bounds what one
# button press can do: the per-device clamp above limits each spray, this
# limits how many of them one request can chain.
#
# The web route must outlast the worst case or the operator is told the service
# is down while hardware is still running, which invites a re-press. Worst case
# wall time is:
#
#   pre_delay (<= 30s, outside the window)
#   + this window (60s, the last device may start just under it)
#   + one spray (<= MAX_ACTUATION_SEC = 60s, it always finishes)
#   = 150s
#
# so the route waits 180s. Changing either number without the other reopens
# that gap.
MAX_GROUP_TEST_FIRE_SEC: float = 60.0
# Ceiling on a detection-driven group window (group_duration_range). A group
# holding a position for minutes is a legitimate choice against a patient
# heron, but it must still be bounded: this is the longest one detection can
# keep devices cycling. Validated at config load, not only clamped at fire
# time, so an operator who asks for more is told rather than silently cut off.
MAX_GROUP_ACTUATION_SEC: float = 300.0
# Cap on the randomised wait before a sequence starts. Clamped in group_fire
# as well as validated in the web config model, because scarguard.yml can be
# hand-edited and an unbounded pre-delay is invisible (nothing is firing yet)
# while still pushing the sequence past the web route's wait.
MAX_PRE_DELAY_SEC: float = 30.0
# Cap on the randomised wait between devices, same reasoning.
MAX_INTER_DELAY_SEC: float = 30.0


def group_test_fire_timeout_sec() -> float:
    """Worst-case wall time of an admin group test-fire, plus a margin.

    Derived rather than hardcoded so the web route cannot drift from the
    deterrent side's real bound. The terms are, in order: the pre-delay before
    anything fires, the firing window itself, one final spray that always runs
    to completion once started, and the inter-device wait the loop performs
    before it notices the window has closed.
    """
    worst = (
        MAX_PRE_DELAY_SEC
        + MAX_GROUP_TEST_FIRE_SEC
        + MAX_ACTUATION_SEC
        + MAX_INTER_DELAY_SEC
    )
    return worst * 1.2


DEFAULT_TEST_FIRE_SEC: float = 3.0

OFF_RETRY_BACKOFF_SEC: tuple[float, ...] = (1.0, 2.0, 4.0)

RECONCILE_INTERVAL_SEC: int = 30


def clamp_duration(
    value: Any,
    *,
    max_sec: float,
    default: float,
    min_sec: float = MIN_ACTUATION_SEC,
) -> float:
    """Coerce *value* to a safe duration in the ``[min_sec, max_sec]`` range.

    Accepts anything and returns a float. Non-numeric, NaN, infinite, and
    out-of-range inputs fall back to *default* (which is then itself
    clamped). Used as a last line of defence - callers should still
    validate at their boundary and return an explicit error instead of
    silently clamping.
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = default
    if math.isnan(num) or math.isinf(num):
        num = default
    if num < min_sec:
        num = min_sec
    if num > max_sec:
        num = max_sec
    return num
