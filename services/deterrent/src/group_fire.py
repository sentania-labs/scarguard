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
from randomizer import build_random_plan, pick_inter_cycle_gap

if TYPE_CHECKING:
    from cloud_controller import TuyaCloudController

logger = logging.getLogger(__name__)

# Fallback when a configured spray duration is unusable (non-numeric, NaN).
# Matches the pre-extraction behaviour of ``main._fire_group``.
DEFAULT_SPRAY_SEC = 3.0

# Reported when a stuck device carries no more specific error.
STUCK_FALLBACK_ERROR = "OFF failed"

# Hard ceiling on rotation cycles, independent of the window. The window is the
# real bound; this exists so a logic error in the exit condition fails loudly
# and finitely instead of spinning forever driving physical hardware.
#
# It IS reachable with a valid configuration: the smallest validated spray is
# 0.5s, so a one-device group with no delays can complete 500 cycles in 250s
# and end a 300s window early. That is an odd thing to configure (a group that
# does nothing but stutter), and stopping early is the safe direction, so the
# ceiling stays and the log says what actually happened rather than blaming a
# bug.
MAX_ROTATION_CYCLES = 500


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
    rotate: bool = False,
    should_continue: Callable[[], bool] | None = None,
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

    ``deadline_sec=None`` means no bound, and *rotate* is meaningless without
    one.

    With *rotate*, a plan that finishes before the window closes is re-rolled
    and fired again, so the group keeps working the position for the whole
    window instead of firing one pass and going quiet. Each cycle re-rolls the
    device selection and the durations, so a heron watching cannot learn the
    pattern, which is the same reason the single pass is randomised at all.

    *should_continue* is consulted before every activation, and returning False
    stops the sequence. It exists because the gates that authorise firing
    (armed, deterrent.enabled, emergency off) are evaluated once before the
    sequence starts, and a sequence can run for minutes. Without it, pressing
    emergency off would send OFF to every device and the sequence would simply
    turn them back on.

    It is never checked mid-activation: an in-flight spray always runs to its
    natural end, so the stop is bounded by one spray and no out-of-band OFF
    races the per-activation watchdog.
    """
    # Rotation without a window would never terminate: the loop's only exit
    # test is "window closed", and with no deadline that is never true. Caught
    # by its own test hanging rather than failing, which is why the rotation
    # tests assert on a fake clock instead of wall time.
    if deadline_sec is None:
        rotate = False

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

    def window_closed() -> bool:
        return (
            deadline_sec is not None
            and (time.monotonic() - fire_start) >= deadline_sec
        )

    aborted = False
    cycle = 0
    while cycle < MAX_ROTATION_CYCLES:
        cycle += 1
        if cycle > 1:
            if should_continue is not None and not should_continue():
                logger.info(
                    "%s: aborted before cycle %d after %d device(s) [rid=%s]",
                    label, cycle, len(actions), request_id,
                )
                break
            # Re-roll: new subset, new durations, new delays.
            selected, durations, inter_delays, _ = build_random_plan(devices, defaults)
            if not selected:
                break
            # build_random_plan always sets inter_delays[0] to 0, which is right
            # within a pass but wrong at a cycle boundary: a small group
            # re-selects the same device and would drive it with no off-time at
            # all, which is what MAX_ACTUATION_SEC exists to prevent. Give the
            # first device of a new cycle a real gap.
            inter_delays = list(inter_delays)
            inter_delays[0] = pick_inter_cycle_gap(defaults)
            logger.debug("%s: rotating, cycle %d [rid=%s]", label, cycle, request_id)

        for i, device in enumerate(selected):
            # Clamped for the same reason the pre-delay and the spray are. This
            # one is also a term in group_test_fire_timeout_sec(), so leaving it
            # unbounded would make that derivation fiction.
            # Gate before the wait as well as after it. The wait can be up to
            # MAX_INTER_DELAY_SEC, so checking only afterwards left the worker
            # parked for that long after the button was pressed, delaying the
            # actuation record and the cooldown even though nothing was firing.
            if should_continue is not None and not should_continue():
                logger.warning(
                    "%s: aborted before the inter-device wait, after %d "
                    "device(s) [rid=%s]",
                    label, len(actions), request_id,
                )
                aborted = True
                break

            inter_delay = min(inter_delays[i], MAX_INTER_DELAY_SEC)
            if inter_delay < inter_delays[i]:
                logger.warning(
                    "Inter-device delay %.1fs exceeds the %.0fs cap, clamping [rid=%s]",
                    inter_delays[i], MAX_INTER_DELAY_SEC, request_id,
                )
            if inter_delay > 0:
                logger.debug("Inter-device delay: %.1fs", inter_delay)
                time.sleep(inter_delay)

            # Checked before EVERY activation, not only at cycle boundaries.
            # One cycle is several devices and tens of seconds: with the
            # shipped ranges, four devices at up to 8s plus three 5s gaps is
            # about 47s. Checking only between cycles meant emergency off
            # switched every device off and then the rest of the current cycle
            # switched them straight back on.
            #
            # Same rule as the window: an activation already in flight runs to
            # its natural end, so the stop is bounded by one spray and no
            # out-of-band OFF races the per-activation watchdog.
            if should_continue is not None and not should_continue():
                logger.warning(
                    "%s: aborted after %d device(s), %d cycle(s) in [rid=%s]",
                    label, len(actions), cycle, request_id,
                )
                aborted = True
                break

            if window_closed():
                logger.info(
                    "%s: window of %.0fs elapsed, stopping after %d device(s) "
                    "across %d cycle(s) [rid=%s]",
                    label, deadline_sec, len(actions), cycle, request_id,
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

        # One pass only unless rotating, and never rotate past the window.
        if aborted or not rotate or window_closed():
            break
    else:
        logger.warning(
            "%s: hit the %d-cycle ceiling with the window still open, stopping "
            "after %d device(s). Either the sprays are configured far shorter "
            "than the window, or the exit condition is wrong [rid=%s]",
            label, MAX_ROTATION_CYCLES, len(actions), request_id,
        )

    return PlanExecution(
        actions=actions,
        pre_delay_sec=pre_delay,
        total_duration_sec=time.monotonic() - t_start,
    )
