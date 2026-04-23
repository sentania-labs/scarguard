"""Deterrent actuation safety bounds.

Hard limits on how long a physical deterrent device (sprinkler, siren,
light) can be held ON by any code path. Enforced at every layer that
accepts a duration: web API, deterrent request handler, randomisation
plan for detection-driven firing, and the cloud controller's own
watchdog. Multiple layers on purpose — if one gets bypassed, the others
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
    clamped). Used as a last line of defence — callers should still
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
