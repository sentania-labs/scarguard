"""Randomisation engine for actuation sequences.

Stateless by design - randomisation IS the anti-habituation strategy.
Each call produces independent random output so wildlife cannot predict
the deterrence pattern.
"""

from __future__ import annotations

import logging
import math
import random

from actuation_models import ActuationDefaults, DeviceConfig
from deterrent_safety import (
    MAX_GROUP_ACTUATION_SEC,
    MAX_INTER_DELAY_SEC,
    MIN_INTER_CYCLE_GAP_SEC,
)

logger = logging.getLogger(__name__)


def build_random_plan(
    devices: list[DeviceConfig],
    defaults: ActuationDefaults,
) -> tuple[list[DeviceConfig], list[float], list[float], float]:
    """Select devices and randomise timing for an actuation sequence.

    Parameters
    ----------
    devices:
        All *enabled* devices eligible for this event.
    defaults:
        Randomisation ranges from config.

    Returns
    -------
    (selected_devices, durations, inter_delays, pre_delay)

    * ``selected_devices`` - ordered list of devices to fire
    * ``durations`` - per-device activation duration in seconds
    * ``inter_delays`` - delay *before* each device (index 0 is always 0)
    * ``pre_delay`` - initial delay before the sequence starts
    """
    if not devices:
        return [], [], [], 0.0

    # How many devices to fire (clamp both ends to available count)
    available = len(devices)
    min_count = max(1, min(defaults.device_count_range[0], available))
    max_count = max(min_count, min(defaults.device_count_range[1], available))
    count = random.randint(min_count, max_count)

    # Pick and shuffle
    selected = random.sample(devices, count)
    random.shuffle(selected)

    # Randomise durations
    dur_min, dur_max = defaults.spray_duration_range
    durations = [random.uniform(dur_min, dur_max) for _ in selected]

    # Randomise inter-device delays (first device has no pre-delay)
    delay_min, delay_max = defaults.inter_device_delay_range
    inter_delays = [0.0] + [random.uniform(delay_min, delay_max) for _ in range(len(selected) - 1)]

    # Pre-delay before the whole sequence
    pre_min, pre_max = defaults.pre_delay_range
    pre_delay = random.uniform(pre_min, pre_max)

    return selected, durations, inter_delays, pre_delay


def pick_group_window(defaults: ActuationDefaults) -> float | None:
    """Pick this firing's group window, or None for a single pass.

    Randomised like every other range so a heron cannot learn how long the
    sprinklers run. Clamped to MAX_GROUP_ACTUATION_SEC: the web layer
    validates the range, but scarguard.yml can be hand-edited and one
    detection must not be able to run the devices indefinitely.
    """
    rng = defaults.group_duration_range
    if not rng or len(rng) != 2:
        return None
    try:
        lo, hi = float(rng[0]), float(rng[1])
    except (TypeError, ValueError):
        logger.warning("Group window range is not numeric, ignoring: %r", rng)
        return None
    # NaN and inf must be rejected, not clamped. Every comparison against NaN is
    # False, so a NaN window would make "has the window closed" permanently
    # False and the sequence would run until the cycle ceiling. Measured at
    # ~115 minutes of continuous firing on the shipped defaults. inf produces
    # NaN here too, via inf + (inf - inf) * r.
    if not (math.isfinite(lo) and math.isfinite(hi)):
        logger.warning(
            "Group window range is not finite, ignoring and firing one pass: %r",
            rng,
        )
        return None
    if hi <= 0:
        return None
    window = random.uniform(min(lo, hi), max(lo, hi))
    if not math.isfinite(window):
        return None
    if window > MAX_GROUP_ACTUATION_SEC:
        logger.warning(
            "Group window %.0fs exceeds the %.0fs cap, clamping",
            window, MAX_GROUP_ACTUATION_SEC,
        )
        window = MAX_GROUP_ACTUATION_SEC
    return window


def pick_inter_cycle_gap(defaults: ActuationDefaults) -> float:
    """Pick the off-time between two rotation cycles.

    Drawn from ``inter_device_delay_range`` like any within-pass gap, but never
    zero: a group small enough to re-select the same device would otherwise
    drive it continuously for the whole window, with no off-time at all. That
    is exactly the duty cycle MAX_ACTUATION_SEC is meant to bound.
    """
    rng = defaults.inter_device_delay_range or [1.0, 5.0]
    try:
        lo, hi = float(rng[0]), float(rng[1])
    except (TypeError, ValueError, IndexError):
        lo, hi = 1.0, 5.0
    if not (math.isfinite(lo) and math.isfinite(hi)):
        lo, hi = 1.0, 5.0
    gap = random.uniform(min(lo, hi), max(lo, hi))
    return max(MIN_INTER_CYCLE_GAP_SEC, min(gap, MAX_INTER_DELAY_SEC))
