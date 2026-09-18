"""Shared group-firing sequence.

Both the detection path (``main._fire_group``) and the admin group test-fire
(``request_handler._handle_test_fire_group``) drive the same hardware through
the same randomised plan. Keeping that sequence in one place means the safety
invariants live in one place too:

* every activation goes through ``controller.activate_device``, which owns the
  watchdog OFF and sets the busy flag the reconcile loop checks before it
  force-OFFs anything;
* every duration is clamped to ``MAX_ACTUATION_SEC`` here as well as in the
  controller, so a tampered or misconfigured range cannot extend a physical
  hold;
* a stuck device is always reported, via a callback because the two callers
  publish to Redis through different clients.

``main`` imports ``RequestHandler``, so the handler cannot import back from
``main``; this module is the seam that lets both share the code.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from actuation_models import ActuationDefaults, DeterrentGroup, DeviceAction, DeviceConfig
from deterrent_safety import (
    MAX_ACTUATION_SEC,
    MAX_INTER_DELAY_SEC,
    MAX_PRE_DELAY_SEC,
    clamp_duration,
)
from pydantic import BaseModel
from randomizer import build_random_plan

if TYPE_CHECKING:
    from cloud_controller import TuyaCloudController

logger = logging.getLogger(__name__)

# Fallback when a configured spray duration is unusable (non-numeric, NaN).
# Matches the pre-extraction behaviour of ``main._fire_group``.
DEFAULT_SPRAY_SEC = 3.0

# Reported when a stuck device carries no more specific error.
STUCK_FALLBACK_ERROR = "OFF failed"


class PlanExecution(BaseModel):
    """Outcome of driving one randomised plan to completion."""

    actions: list[DeviceAction]
    pre_delay_sec: float
    total_duration_sec: float

    @property
    def successes(self) -> int:
        return sum(1 for a in self.actions if a.success)


def resolve_group_devices(
    group: DeterrentGroup,
    registry: list[DeviceConfig],
) -> list[DeviceConfig]:
    """Return the enabled devices referenced by *group*, preserving registry order."""
    wanted = set(group.devices)
    return [d for d in registry if d.enabled and d.name in wanted]


def execute_plan(
    controller: TuyaCloudController,
    devices: list[DeviceConfig],
    defaults: ActuationDefaults,
    *,
    request_id: str,
    event_type: str,
    label: str,
    on_stuck: Callable[[DeviceConfig, str], None],
    deadline_sec: float | None = None,
) -> PlanExecution:
    """Build a randomised plan over *devices* and fire it, device by device.

    *label* only appears in log lines, so the detection path and an admin
    test-fire are distinguishable in the log stream. *on_stuck* is invoked once
    per device that reported ON-succeeded-but-OFF-failed; the caller decides how
    to publish it.

    *deadline_sec* bounds the firing window. It is checked immediately before
    each activation starts, never during one, so an activation already in
    flight always runs to its natural end and no further device is picked up.
    No out-of-band OFF is ever sent, so nothing races the per-activation
    watchdog.

    Two details the obvious implementation gets wrong:

    * the clock starts after the pre-delay, not before, so a pre_delay_range
      at or above the window cannot consume it and yield a zero-device
      sequence. ``total_duration_sec`` still spans the pre-delay, because it
      is persisted to the audit record and must mean wall time;
    * the check sits after the inter-device sleep rather than before it,
      so overshoot past the window is bounded by one spray duration rather
      than by a delay plus a spray.

    ``None`` means no bound, which is what the detection path uses.
    """
    selected, durations, inter_delays, pre_delay = build_random_plan(devices, defaults)

    # Clamped for the same reason durations are: the web layer validates the
    # range, but scarguard.yml can be hand-edited and this service must not
    # take an unbounded wait on the word of a config file. An unclamped
    # pre-delay is invisible to the operator (nothing is firing yet) and pushes
    # the whole sequence past the web route's wait.
    if pre_delay > MAX_PRE_DELAY_SEC:
        logger.warning(
            "Pre-delay %.1fs exceeds the %.0fs cap, clamping [rid=%s]",
            pre_delay, MAX_PRE_DELAY_SEC, request_id,
        )
        pre_delay = MAX_PRE_DELAY_SEC

    t_start = time.monotonic()
    actions: list[DeviceAction] = []

    if pre_delay > 0:
        logger.debug("Pre-delay: %.1fs", pre_delay)
        time.sleep(pre_delay)

    # The firing window starts once waiting is done (see docstring).
    fire_start = time.monotonic()

    for i, device in enumerate(selected):
        # Clamped for the same reason the pre-delay and the spray are. This
        # one is also a term in group_test_fire_timeout_sec(), so leaving it
        # unbounded would make that derivation fiction.
        inter_delay = min(inter_delays[i], MAX_INTER_DELAY_SEC)
        if inter_delay < inter_delays[i]:
            logger.warning(
                "Inter-device delay %.1fs exceeds the %.0fs cap, clamping [rid=%s]",
                inter_delays[i], MAX_INTER_DELAY_SEC, request_id,
            )
        if inter_delay > 0:
            logger.debug("Inter-device delay: %.1fs", inter_delay)
            time.sleep(inter_delay)

        if deadline_sec is not None and (time.monotonic() - fire_start) >= deadline_sec:
            logger.info(
                "%s: window of %.0fs elapsed, stopping after %d of %d device(s) [rid=%s]",
                label, deadline_sec, len(actions), len(selected), request_id,
            )
            break

        # Defence-in-depth clamp - the randomizer reads spray_duration_range
        # from config; a misconfigured or tampered config can't drive the
        # physical hold beyond MAX_ACTUATION_SEC. The controller clamps too.
        duration = clamp_duration(
            durations[i],
            max_sec=MAX_ACTUATION_SEC,
            default=DEFAULT_SPRAY_SEC,
        )
        logger.info(
            "%s: firing device %s (%s) for %.1fs [rid=%s]",
            label, device.name, device.type, duration, request_id,
        )
        result = controller.activate_device(
            device, duration,
            request_id=request_id,
            event_type=event_type,
        )
        actions.append(DeviceAction(
            device_name=device.name,
            device_id=device.device_id,
            device_type=device.type,
            duration_sec=duration,
            delay_before_sec=inter_delay,
            success=result.success,
            error=result.error,
            cloud_ack_ms=result.on_ack_ms,
            off_attempts=result.off_attempts,
            stuck=result.stuck,
        ))
        if result.stuck:
            on_stuck(device, result.error or STUCK_FALLBACK_ERROR)

    return PlanExecution(
        actions=actions,
        pre_delay_sec=pre_delay,
        total_duration_sec=time.monotonic() - t_start,
    )
