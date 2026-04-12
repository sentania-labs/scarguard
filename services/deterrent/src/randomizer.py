"""Randomisation engine for actuation sequences.

Stateless by design — randomisation IS the anti-habituation strategy.
Each call produces independent random output so wildlife cannot predict
the deterrence pattern.
"""

from __future__ import annotations

import random

from actuation_models import ActuationDefaults, DeviceConfig


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

    * ``selected_devices`` — ordered list of devices to fire
    * ``durations`` — per-device activation duration in seconds
    * ``inter_delays`` — delay *before* each device (index 0 is always 0)
    * ``pre_delay`` — initial delay before the sequence starts
    """
    if not devices:
        return [], [], [], 0.0

    # How many devices to fire
    min_count = max(1, defaults.device_count_range[0])
    max_count = min(len(devices), defaults.device_count_range[1])
    count = random.randint(min_count, max(min_count, max_count))

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
