"""Tests for the shared group-firing sequence.

Both the detection path and the admin group test-fire drive hardware through
``execute_plan``, so the safety properties asserted here (every activation is
clamped, every activation goes through the controller, a stuck device is always
reported) cover both callers at once.
"""

from __future__ import annotations

from typing import Any, Callable

import group_fire as group_fire_module
import pytest
from actuation_models import ActuationDefaults, DeterrentGroup, DeviceConfig
from atomic_ref import AtomicRef
from cloud_controller import ActivationResult
from deterrent_safety import (
    MAX_ACTUATION_SEC,
    MAX_INTER_DELAY_SEC,
    MAX_PRE_DELAY_SEC,
    MIN_INTER_CYCLE_GAP_SEC,
)
from group_fire import execute_plan, resolve_group_devices


def _device(name: str, *, enabled: bool = True) -> DeviceConfig:
    return DeviceConfig(name=name, device_id=f"id-{name}", type="sprinkler", enabled=enabled)


class FakeController:
    """Records every activation instead of talking to Tuya Cloud."""

    def __init__(self, *, stuck_on: set[str] | None = None, fail_on: set[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._stuck_on = stuck_on or set()
        self._fail_on = fail_on or set()

    def activate_device(
        self,
        device: DeviceConfig,
        duration: float,
        *,
        request_id: str,
        event_type: str,
        should_continue: Callable[[], bool] | None = None,
    ) -> ActivationResult:
        self.calls.append({
            "device": device.name,
            "duration": duration,
            "request_id": request_id,
            "event_type": event_type,
        })
        # success and stuck are derived properties, so build the real states:
        #   ON ok + OFF ok    -> success
        #   ON ok + OFF fail  -> stuck (device may be physically still on)
        #   ON fail           -> neither; OFF was never attempted
        if device.name in self._fail_on:
            return ActivationResult(
                on_success=False, off_success=None, error="boom",
                on_ack_ms=None, off_attempts=0,
            )
        if device.name in self._stuck_on:
            return ActivationResult(
                on_success=True, off_success=False, error=None,
                on_ack_ms=12.0, off_attempts=3,
            )
        return ActivationResult(
            on_success=True, off_success=True, error=None,
            on_ack_ms=12.0, off_attempts=1,
        )


def _run(
    controller: FakeController,
    devices: list[DeviceConfig],
    defaults: ActuationDefaults,
    stuck_sink: list[tuple[str, str]] | None = None,
) -> Any:
    sink = stuck_sink if stuck_sink is not None else []
    return execute_plan(
        controller,
        devices,
        defaults,
        request_id="rid-test",
        event_type="test_fire_group",
        label="Test",
        on_stuck=lambda d, e: sink.append((d.name, e)),
    )


def _collapse(trace: list) -> list:
    """Merge adjacent ("sleep", x) entries into one, summing the values.

    Waits are slept in slices (see group_fire._wait) so that a revoked
    authorisation is noticed promptly. A trace therefore contains many small
    sleeps where it once contained one. The totals are unchanged, and the
    totals are what these tests assert.
    """
    out: list = []
    for kind, value in trace:
        if kind == "sleep" and out and out[-1][0] == "sleep":
            out[-1] = ("sleep", round(out[-1][1] + value, 6))
        else:
            out.append((kind, value))
    return out


class TestResolveGroupDevices:
    def test_returns_only_enabled_members(self) -> None:
        registry = [_device("a"), _device("b", enabled=False), _device("c")]
        group = DeterrentGroup(name="g", devices=["a", "b", "c"])
        assert [d.name for d in resolve_group_devices(group, registry)] == ["a", "c"]

    def test_ignores_names_not_in_registry(self) -> None:
        registry = [_device("a")]
        group = DeterrentGroup(name="g", devices=["a", "ghost"])
        assert [d.name for d in resolve_group_devices(group, registry)] == ["a"]

    def test_preserves_registry_order_not_group_order(self) -> None:
        registry = [_device("a"), _device("b")]
        group = DeterrentGroup(name="g", devices=["b", "a"])
        assert [d.name for d in resolve_group_devices(group, registry)] == ["a", "b"]

    def test_empty_group_resolves_empty(self) -> None:
        assert resolve_group_devices(DeterrentGroup(name="g"), [_device("a")]) == []


class TestExecutePlan:
    def test_fires_every_selected_device_through_the_controller(self) -> None:
        devices = [_device(f"v{i}") for i in range(3)]
        defaults = ActuationDefaults(
            device_count_range=[3, 3],
            spray_duration_range=[1.0, 1.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
        )
        controller = FakeController()
        execution = _run(controller, devices, defaults)

        assert len(controller.calls) == 3
        assert len(execution.actions) == 3
        assert execution.successes == 3

    def test_duration_is_clamped_to_max_actuation_sec(self) -> None:
        """A tampered or misconfigured range must not extend the physical hold."""
        devices = [_device("v1")]
        defaults = ActuationDefaults(
            device_count_range=[1, 1],
            spray_duration_range=[9999.0, 9999.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
        )
        controller = FakeController()
        execution = _run(controller, devices, defaults)

        assert controller.calls[0]["duration"] == MAX_ACTUATION_SEC
        assert execution.actions[0].duration_sec == MAX_ACTUATION_SEC

    def test_event_type_and_request_id_reach_the_controller(self) -> None:
        """The audit trail depends on both being carried through unchanged."""
        defaults = ActuationDefaults(
            device_count_range=[1, 1],
            spray_duration_range=[1.0, 1.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
        )
        controller = FakeController()
        _run(controller, [_device("v1")], defaults)

        assert controller.calls[0]["event_type"] == "test_fire_group"
        assert controller.calls[0]["request_id"] == "rid-test"

    def test_stuck_device_is_reported_once_per_device(self) -> None:
        devices = [_device("v1"), _device("v2")]
        defaults = ActuationDefaults(
            device_count_range=[2, 2],
            spray_duration_range=[1.0, 1.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
        )
        sink: list[tuple[str, str]] = []
        controller = FakeController(stuck_on={"v2"})
        execution = _run(controller, devices, defaults, sink)

        assert sink == [("v2", "OFF failed")]
        # build_random_plan shuffles, so assert on content rather than position.
        stuck_names = [a.device_name for a in execution.actions if a.stuck]
        assert stuck_names == ["v2"]
        assert len(execution.actions) == 2

    def test_a_failing_device_does_not_abort_the_rest(self) -> None:
        """One dead valve must not stop the others from firing."""
        devices = [_device("v1"), _device("v2"), _device("v3")]
        defaults = ActuationDefaults(
            device_count_range=[3, 3],
            spray_duration_range=[1.0, 1.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
        )
        controller = FakeController(fail_on={"v2"})
        execution = _run(controller, devices, defaults)

        assert len(controller.calls) == 3
        assert execution.successes == 2

    def test_no_devices_fires_nothing(self) -> None:
        controller = FakeController()
        execution = _run(controller, [], ActuationDefaults())

        assert controller.calls == []
        assert execution.actions == []
        assert execution.successes == 0


class TestTimingIsPreserved:
    """Pins the four properties a mutation run found uncovered.

    The refactor that extracted execute_plan out of main._fire_group was
    behaviour-preserving, but nothing stopped the next edit from silently
    changing pre_delay placement, index alignment, the persisted
    delay_before_sec, or the span total_duration_sec measures. The last one
    matters most: it is written to the actuation audit record.
    """

    @staticmethod
    def _timed_defaults(pre: float, inter: float) -> ActuationDefaults:
        return ActuationDefaults(
            device_count_range=[3, 3],
            spray_duration_range=[0.0, 0.0],
            inter_device_delay_range=[inter, inter],
            pre_delay_range=[pre, pre],
        )

    def test_pre_delay_is_actually_slept(self, monkeypatch: Any) -> None:
        slept: list[float] = []
        monkeypatch.setattr("group_fire.time.sleep", lambda s: slept.append(s))
        devices = [_device(f"v{i}") for i in range(3)]
        _run(FakeController(), devices, self._timed_defaults(2.5, 0.0))

        assert round(sum(slept), 6) == 2.5, f"pre_delay was not slept: {slept}"

    def test_inter_delays_are_index_aligned(self, monkeypatch: Any) -> None:
        """Device 0 never waits; device i waits inter_delays[i].

        Asserted on the interleaving rather than on totals: with a uniform
        delay range, an off-by-one shifts which device waits without changing
        how many sleeps happen, so counting alone cannot see it.
        """
        trace: list[tuple[str, Any]] = []
        monkeypatch.setattr("group_fire.time.sleep", lambda s: trace.append(("sleep", s)))
        controller = FakeController()
        real = controller.activate_device

        def traced(device: DeviceConfig, duration: float, **kw: Any) -> Any:
            trace.append(("fire", device.name))
            return real(device, duration, **kw)

        controller.activate_device = traced  # type: ignore[method-assign]
        devices = [_device(f"v{i}") for i in range(3)]
        execution = _run(controller, devices, self._timed_defaults(0.0, 1.5))

        merged = _collapse(trace)
        # No pre-delay, so the very first thing that happens is a firing.
        assert merged[0][0] == "fire", f"device 0 waited before firing: {merged[:2]}"
        # Thereafter strictly alternating: sleep, fire, sleep, fire.
        assert [kind for kind, _ in merged] == [
            "fire", "sleep", "fire", "sleep", "fire",
        ]
        assert [v for k, v in merged if k == "sleep"] == [1.5, 1.5]
        assert execution.actions[0].delay_before_sec == 0.0
        assert [a.delay_before_sec for a in execution.actions[1:]] == [1.5, 1.5]

    def test_delay_before_sec_is_recorded_not_zeroed(self, monkeypatch: Any) -> None:
        """This lands in the actuation audit record and must be the real value."""
        monkeypatch.setattr("group_fire.time.sleep", lambda s: None)
        devices = [_device(f"v{i}") for i in range(3)]
        execution = _run(FakeController(), devices, self._timed_defaults(0.0, 2.0))

        assert sum(a.delay_before_sec for a in execution.actions) == 4.0

    def test_total_duration_includes_the_pre_delay(self, monkeypatch: Any) -> None:
        """t_start is taken BEFORE the pre-delay sleep; moving it changes the
        meaning of a persisted audit field."""
        clock = {"t": 0.0}
        monkeypatch.setattr("group_fire.time.monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            "group_fire.time.sleep", lambda s: clock.__setitem__("t", clock["t"] + s),
        )
        devices = [_device("v1")]
        defaults = ActuationDefaults(
            device_count_range=[1, 1],
            spray_duration_range=[0.0, 0.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[5.0, 5.0],
        )
        execution = _run(FakeController(), devices, defaults)

        assert execution.total_duration_sec == 5.0


class TestDeadline:
    """The window is checked before each device, never during one."""

    @staticmethod
    def _defaults() -> ActuationDefaults:
        return ActuationDefaults(
            device_count_range=[4, 4],
            spray_duration_range=[10.0, 10.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
        )

    def _run_with_clock(self, deadline: float, monkeypatch: Any) -> Any:
        clock = {"t": 0.0}
        monkeypatch.setattr("group_fire.time.monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            "group_fire.time.sleep", lambda s: clock.__setitem__("t", clock["t"] + s),
        )
        controller = FakeController()

        # Each activation advances the clock by its duration, as a real hold would.
        real = controller.activate_device

        def timed(device: DeviceConfig, duration: float, **kw: Any) -> Any:
            clock["t"] += duration
            return real(device, duration, **kw)

        controller.activate_device = timed  # type: ignore[method-assign]
        devices = [_device(f"v{i}") for i in range(4)]
        execution = execute_plan(
            controller, devices, self._defaults(),
            request_id="rid", event_type="test_fire_group", label="T",
            on_stuck=lambda d, e: None,
            deadline_sec=deadline,
        )
        return controller, execution

    def test_stops_picking_up_devices_once_the_window_elapses(self, monkeypatch: Any) -> None:
        controller, execution = self._run_with_clock(25.0, monkeypatch)
        # 3 devices x 10s: the third starts at t=20 (under 25), the fourth at
        # t=30 and is never picked up.
        assert len(controller.calls) == 3
        assert len(execution.actions) == 3

    def test_in_flight_device_always_finishes(self, monkeypatch: Any) -> None:
        """Overshoot is bounded by one spray; no out-of-band OFF is sent."""
        controller, execution = self._run_with_clock(5.0, monkeypatch)
        assert len(controller.calls) == 1
        assert execution.actions[0].duration_sec == 10.0
        assert execution.total_duration_sec == 10.0

    def test_no_deadline_fires_the_whole_plan(self, monkeypatch: Any) -> None:
        clock = {"t": 0.0}
        monkeypatch.setattr("group_fire.time.monotonic", lambda: clock["t"])
        monkeypatch.setattr("group_fire.time.sleep", lambda s: None)
        controller = FakeController()
        devices = [_device(f"v{i}") for i in range(4)]
        execution = execute_plan(
            controller, devices, self._defaults(),
            request_id="rid", event_type="detection", label="T",
            on_stuck=lambda d, e: None,
        )
        assert len(execution.actions) == 4


class TestDeadlineEdges:
    """The two properties the obvious deadline implementation gets wrong."""

    @staticmethod
    def _clock(monkeypatch: Any) -> dict[str, float]:
        clock = {"t": 0.0}
        monkeypatch.setattr("group_fire.time.monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            "group_fire.time.sleep", lambda s: clock.__setitem__("t", clock["t"] + s),
        )
        return clock

    @staticmethod
    def _timed_controller(clock: dict[str, float]) -> FakeController:
        controller = FakeController()
        real = controller.activate_device

        def timed(device: DeviceConfig, duration: float, **kw: Any) -> Any:
            clock["t"] += duration
            return real(device, duration, **kw)

        controller.activate_device = timed  # type: ignore[method-assign]
        return controller

    def test_overshoot_is_one_spray_even_with_long_inter_delays(self, monkeypatch: Any) -> None:
        """The check sits AFTER the inter-device sleep.

        Checking before it instead would make overshoot delay+spray, not spray.
        """
        clock = self._clock(monkeypatch)
        controller = self._timed_controller(clock)
        defaults = ActuationDefaults(
            device_count_range=[4, 4],
            spray_duration_range=[10.0, 10.0],
            inter_device_delay_range=[20.0, 20.0],
            pre_delay_range=[0.0, 0.0],
        )
        execution = execute_plan(
            controller, [_device(f"v{i}") for i in range(4)], defaults,
            request_id="rid", event_type="test_fire_group", label="T",
            on_stuck=lambda d, e: None, deadline_sec=25.0,
        )
        # Window 25s: device 0 fires at t=0, device 1 at t=30 after its delay,
        # which is past the window, so it never starts.
        assert len(execution.actions) == 1
        assert execution.total_duration_sec <= 25.0 + 10.0

    def test_pre_delay_is_clamped_and_cannot_consume_the_window(self, monkeypatch: Any) -> None:
        """A long pre-delay must be capped AND must not eat the firing window.

        Two separate protections. The clamp bounds an unbounded value that a
        hand-edited scarguard.yml can still carry past the web validators; the
        window starting after the pre-delay is what stops even a legal
        pre-delay from yielding a zero-device sequence.
        """
        clock = self._clock(monkeypatch)
        controller = self._timed_controller(clock)
        defaults = ActuationDefaults(
            device_count_range=[2, 2],
            spray_duration_range=[5.0, 5.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[100.0, 100.0],
        )
        execution = execute_plan(
            controller, [_device("v1"), _device("v2")], defaults,
            request_id="rid", event_type="test_fire_group", label="T",
            on_stuck=lambda d, e: None, deadline_sec=30.0,
        )
        assert len(execution.actions) == 2, "pre-delay ate the firing window"
        # 100s requested, clamped to MAX_PRE_DELAY_SEC, then two 5s sprays.
        assert execution.pre_delay_sec == MAX_PRE_DELAY_SEC
        # total_duration_sec still spans the pre-delay: it is an audit field.
        assert execution.total_duration_sec == MAX_PRE_DELAY_SEC + 10.0


class TestEveryWaitIsBounded:
    """Each term in group_test_fire_timeout_sec() must actually be enforced.

    A term that nothing clamps makes the derived route timeout fiction: the
    web page gives up and reports the service down while hardware is still
    running, which is the state that invites a re-press.
    """

    def test_inter_device_delay_is_clamped(self, monkeypatch: Any) -> None:
        slept: list[float] = []
        monkeypatch.setattr("group_fire.time.sleep", lambda s: slept.append(s))
        defaults = ActuationDefaults(
            device_count_range=[3, 3],
            spray_duration_range=[0.0, 0.0],
            inter_device_delay_range=[300.0, 300.0],
            pre_delay_range=[0.0, 0.0],
        )
        execution = _run(FakeController(), [_device(f"v{i}") for i in range(3)], defaults)

        assert slept, "no delay was slept at all"
        assert max(slept) <= MAX_INTER_DELAY_SEC, f"unbounded inter-device wait: {slept}"
        # The audit record must show what was actually waited, not what was asked.
        assert max(a.delay_before_sec for a in execution.actions) <= MAX_INTER_DELAY_SEC

    def test_pre_delay_is_clamped(self, monkeypatch: Any) -> None:
        monkeypatch.setattr("group_fire.time.sleep", lambda s: None)
        defaults = ActuationDefaults(
            device_count_range=[1, 1],
            spray_duration_range=[0.0, 0.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[500.0, 500.0],
        )
        execution = _run(FakeController(), [_device("v1")], defaults)
        assert execution.pre_delay_sec == MAX_PRE_DELAY_SEC


class TestRotation:
    """A group with a window keeps working the position until it closes.

    The point of #189: a heron that waits out a three-second burst has not
    been deterred. Without rotation the group fires one pass and goes quiet
    for the whole cooldown.
    """

    @staticmethod
    def _clock(monkeypatch: Any) -> dict[str, float]:
        clock = {"t": 0.0}
        monkeypatch.setattr("group_fire.time.monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            "group_fire.time.sleep", lambda s: clock.__setitem__("t", clock["t"] + s),
        )
        return clock

    @staticmethod
    def _timed(controller: FakeController, clock: dict[str, float]) -> FakeController:
        real = controller.activate_device

        def timed(device: DeviceConfig, duration: float, **kw: Any) -> Any:
            clock["t"] += duration
            return real(device, duration, **kw)

        controller.activate_device = timed  # type: ignore[method-assign]
        return controller

    @staticmethod
    def _defaults() -> ActuationDefaults:
        return ActuationDefaults(
            device_count_range=[2, 2],
            spray_duration_range=[5.0, 5.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
        )

    def _fire(self, monkeypatch: Any, *, window: float | None, rotate: bool) -> Any:
        clock = self._clock(monkeypatch)
        controller = self._timed(FakeController(), clock)
        execution = execute_plan(
            controller, [_device("v1"), _device("v2")], self._defaults(),
            request_id="rid", event_type="detection", label="T",
            on_stuck=lambda d, e: None,
            deadline_sec=window, rotate=rotate,
        )
        return controller, execution

    def test_without_a_window_it_is_one_pass(self, monkeypatch: Any) -> None:
        """The pre-v1.17 behaviour must be exactly preserved."""
        controller, execution = self._fire(monkeypatch, window=None, rotate=False)
        assert len(execution.actions) == 2

    def test_with_a_window_it_keeps_cycling(self, monkeypatch: Any) -> None:
        # 2 devices x 5s = 10s per cycle; a 30s window fits three cycles.
        controller, execution = self._fire(monkeypatch, window=30.0, rotate=True)
        assert len(execution.actions) == 6, "group stopped after one pass"

    def test_rotation_stops_at_the_window(self, monkeypatch: Any) -> None:
        controller, execution = self._fire(monkeypatch, window=12.0, rotate=True)
        # Cycle 1 fires both devices, ending at t=10 (under 12, so cycle 2
        # starts). Cycle 2 waits the mandatory inter-cycle gap to t=12, by
        # which point the window has closed, so nothing more fires.
        assert len(execution.actions) == 2
        assert execution.total_duration_sec == 12.0

    def test_cycles_are_separated_by_a_real_gap(self, monkeypatch: Any) -> None:
        """A small group must not drive one device with no off-time.

        build_random_plan always gives the first device of a pass a zero delay,
        which is right within a pass and wrong at a cycle boundary: a
        one-device group would otherwise be held on continuously for the whole
        window, defeating the duty cycle MAX_ACTUATION_SEC exists to bound.
        """
        clock = self._clock(monkeypatch)
        controller = self._timed(FakeController(), clock)
        defaults = ActuationDefaults(
            device_count_range=[1, 1],
            spray_duration_range=[5.0, 5.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
        )
        execution = execute_plan(
            controller, [_device("solo")], defaults,
            request_id="rid", event_type="detection", label="T",
            on_stuck=lambda d, e: None, deadline_sec=30.0, rotate=True,
        )
        gaps = [a.delay_before_sec for a in execution.actions[1:]]
        assert gaps, "did not rotate"
        assert all(g >= MIN_INTER_CYCLE_GAP_SEC for g in gaps), (
            f"device driven with no off-time between cycles: {gaps}"
        )

    def test_an_in_flight_spray_still_finishes(self, monkeypatch: Any) -> None:
        """Overshoot stays bounded by one spray; no out-of-band OFF is sent."""
        controller, execution = self._fire(monkeypatch, window=1.0, rotate=True)
        assert len(execution.actions) == 1
        assert execution.actions[0].duration_sec == 5.0

    def test_rotate_without_a_window_is_still_one_pass(self, monkeypatch: Any) -> None:
        """rotate is meaningless without a deadline and must not loop forever."""
        controller, execution = self._fire(monkeypatch, window=None, rotate=True)
        assert len(execution.actions) == 2


class TestPickGroupWindow:
    def test_none_when_unset(self) -> None:
        from randomizer import pick_group_window
        assert pick_group_window(ActuationDefaults()) is None

    def test_none_when_zero(self) -> None:
        from randomizer import pick_group_window
        d = ActuationDefaults(group_duration_range=[0.0, 0.0])
        assert pick_group_window(d) is None

    def test_within_the_configured_range(self) -> None:
        from randomizer import pick_group_window
        d = ActuationDefaults(group_duration_range=[10.0, 20.0])
        for _ in range(50):
            w = pick_group_window(d)
            assert w is not None and 10.0 <= w <= 20.0

    def test_clamped_to_the_ceiling(self) -> None:
        """One detection must not be able to run the devices indefinitely."""
        from deterrent_safety import MAX_GROUP_ACTUATION_SEC
        from randomizer import pick_group_window
        d = ActuationDefaults(group_duration_range=[99999.0, 99999.0])
        assert pick_group_window(d) == MAX_GROUP_ACTUATION_SEC


class TestRotationCanBeStopped:
    """The gates that authorise firing are checked once, before the sequence.

    A window can now run for minutes, so an operator who disarms, disables the
    deterrent, or hits emergency off during one must actually stop it. Without
    the abort hook the force-off switches every device off and the next cycle
    switches them straight back on.
    """

    @staticmethod
    def _defaults() -> ActuationDefaults:
        return ActuationDefaults(
            device_count_range=[2, 2],
            spray_duration_range=[5.0, 5.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
        )

    def _fire(self, monkeypatch: Any, should_continue: Any) -> Any:
        clock = {"t": 0.0}
        monkeypatch.setattr("group_fire.time.monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            "group_fire.time.sleep", lambda s: clock.__setitem__("t", clock["t"] + s),
        )
        controller = FakeController()
        real = controller.activate_device

        def timed(device: DeviceConfig, duration: float, **kw: Any) -> Any:
            clock["t"] += duration
            return real(device, duration, **kw)

        controller.activate_device = timed  # type: ignore[method-assign]
        return execute_plan(
            controller, [_device("v1"), _device("v2")], self._defaults(),
            request_id="rid", event_type="detection", label="T",
            on_stuck=lambda d, e: None,
            deadline_sec=300.0, rotate=True, should_continue=should_continue,
        )

    def test_revoked_authorisation_stops_immediately(self, monkeypatch: Any) -> None:
        """Before the next activation, not at the next cycle boundary."""
        execution = self._fire(monkeypatch, lambda: False)
        assert len(execution.actions) == 0

    def test_still_authorised_keeps_going(self, monkeypatch: Any) -> None:
        execution = self._fire(monkeypatch, lambda: True)
        assert len(execution.actions) > 2

    def test_no_hook_means_no_abort(self, monkeypatch: Any) -> None:
        """The detection path passes one; anything else must not change."""
        execution = self._fire(monkeypatch, None)
        assert len(execution.actions) > 2

    def test_abort_stops_mid_cycle_not_just_between_cycles(self, monkeypatch: Any) -> None:
        """The case that made this worth fixing.

        One cycle is several devices and tens of seconds. Checking only at
        cycle boundaries meant emergency off switched every device off and the
        rest of the current cycle switched them straight back on.

        Keyed on activations rather than callback invocations, because the
        gate is consulted more than once per device (before the inter-device
        wait and again before firing).
        """
        fired: list[str] = []
        execution = self._fire_counting(monkeypatch, fired, revoke_after=1)
        # Cycle one has two devices; the second must not fire.
        assert len(execution.actions) == 1
        assert len(fired) == 1

    def test_an_activation_in_flight_still_finishes(self, monkeypatch: Any) -> None:
        """Bounded by one spray, so no out-of-band OFF races the watchdog."""
        fired: list[str] = []
        execution = self._fire_counting(monkeypatch, fired, revoke_after=1)
        assert [a.duration_sec for a in execution.actions] == [5.0], (
            "the activation in flight was cut short"
        )

    def _fire_counting(self, monkeypatch: Any, fired: list, revoke_after: int) -> Any:
        """Fire with authorisation revoked once *revoke_after* devices have run."""
        clock = {"t": 0.0}
        monkeypatch.setattr("group_fire.time.monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            "group_fire.time.sleep", lambda s: clock.__setitem__("t", clock["t"] + s),
        )
        controller = FakeController()
        real = controller.activate_device

        def timed(device: DeviceConfig, duration: float, **kw: Any) -> Any:
            fired.append(device.name)
            clock["t"] += duration
            return real(device, duration, **kw)

        controller.activate_device = timed  # type: ignore[method-assign]
        return execute_plan(
            controller, [_device("v1"), _device("v2")], self._defaults(),
            request_id="rid", event_type="detection", label="T",
            on_stuck=lambda d, e: None,
            deadline_sec=300.0, rotate=True,
            should_continue=lambda: len(fired) < revoke_after,
        )



class TestCycleCeiling:
    def test_ceiling_bounds_a_pathological_window(self, monkeypatch: Any) -> None:
        """The backstop when the window bound itself fails.

        Without it a non-finite window made every comparison False and the
        sequence ran for roughly two hours.

        Run on a thread with a join timeout rather than called directly: if
        the ceiling is removed this loops forever, and a hanging test burns
        the whole CI job instead of reporting a failure.
        """
        import threading

        from group_fire import MAX_ROTATION_CYCLES

        monkeypatch.setattr("group_fire.time.sleep", lambda s: None)
        monkeypatch.setattr("group_fire.time.monotonic", lambda: 0.0)  # frozen
        defaults = ActuationDefaults(
            device_count_range=[1, 1],
            spray_duration_range=[0.5, 0.5],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
        )
        result: dict[str, Any] = {}

        def run() -> None:
            result["execution"] = execute_plan(
                FakeController(), [_device("v1")], defaults,
                request_id="rid", event_type="detection", label="T",
                on_stuck=lambda d, e: None, deadline_sec=999999.0, rotate=True,
            )

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=20.0)
        assert not t.is_alive(), (
            "rotation did not terminate: the cycle ceiling is the only thing "
            "bounding a window that never closes"
        )
        assert len(result["execution"].actions) == MAX_ROTATION_CYCLES

    def test_ceiling_is_a_sane_value(self) -> None:
        from group_fire import MAX_ROTATION_CYCLES
        assert 100 <= MAX_ROTATION_CYCLES <= 2000, (
            "too low truncates real windows, too high stops bounding anything"
        )


class TestPreDelayAppliesOnce:
    def test_pre_delay_is_not_repeated_per_cycle(self, monkeypatch: Any) -> None:
        """Repeating it would insert dead air before every cycle.

        Asserted on the interleaving rather than the total: rotation adds a
        mandatory gap at each cycle boundary, so the sum of all sleeps is not
        the pre-delay. What matters is that the 7s wait happens once, before
        any firing, and never again.
        """
        trace: list = []
        monkeypatch.setattr("group_fire.time.sleep", lambda s: trace.append(("sleep", s)))
        clock = {"t": 0.0}
        monkeypatch.setattr("group_fire.time.monotonic", lambda: clock["t"])
        controller = FakeController()
        real = controller.activate_device

        def timed(device: DeviceConfig, duration: float, **kw: Any) -> Any:
            trace.append(("fire", device.name))
            clock["t"] += duration
            return real(device, duration, **kw)

        controller.activate_device = timed  # type: ignore[method-assign]
        defaults = ActuationDefaults(
            device_count_range=[1, 1],
            spray_duration_range=[5.0, 5.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[7.0, 7.0],
        )
        execute_plan(
            controller, [_device("v1")], defaults,
            request_id="rid", event_type="detection", label="T",
            on_stuck=lambda d, e: None, deadline_sec=30.0, rotate=True,
        )
        merged = _collapse(trace)
        assert merged[0] == ("sleep", 7.0), f"pre-delay not slept first: {merged[:2]}"
        assert merged[1][0] == "fire"
        later_sleeps = [v for k, v in merged[2:] if k == "sleep"]
        assert 7.0 not in later_sleeps, f"pre-delay repeated: {merged}"


class TestNonFiniteWindowIsRejected:
    """NaN and inf must be refused, not clamped.

    Every comparison against NaN is False, so a NaN window makes "has the
    window closed" permanently False and the sequence runs until the cycle
    ceiling. Measured at roughly two hours of continuous firing on the shipped
    defaults before this was fixed. inf produces NaN here too, via
    inf + (inf - inf) * r.
    """

    @pytest.mark.parametrize("rng", [
        [float("nan"), float("nan")],
        [float("inf"), float("inf")],
        [float("nan"), 50.0],
        [50.0, float("inf")],
        [float("-inf"), float("inf")],
    ])
    def test_returns_none(self, rng: list[float]) -> None:
        from randomizer import pick_group_window
        assert pick_group_window(ActuationDefaults(group_duration_range=rng)) is None

    def test_a_non_finite_window_does_not_rotate(self, monkeypatch: Any) -> None:
        """End to end: a NaN range must produce one pass, not a marathon."""
        import threading

        monkeypatch.setattr("group_fire.time.sleep", lambda s: None)
        monkeypatch.setattr("group_fire.time.monotonic", lambda: 0.0)
        from randomizer import pick_group_window

        defaults = ActuationDefaults(
            device_count_range=[1, 1],
            spray_duration_range=[0.5, 0.5],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
            group_duration_range=[float("nan"), float("nan")],
        )
        window = pick_group_window(defaults)
        result: dict[str, Any] = {}

        def run() -> None:
            result["execution"] = execute_plan(
                FakeController(), [_device("v1")], defaults,
                request_id="rid", event_type="detection", label="T",
                on_stuck=lambda d, e: None,
                deadline_sec=window, rotate=window is not None,
            )

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=20.0)
        assert not t.is_alive(), "a non-finite window ran away"
        assert len(result["execution"].actions) == 1, "should be a single pass"


class TestEmptyPlanMidRotation:
    def test_an_empty_re_roll_ends_the_rotation(self, monkeypatch: Any) -> None:
        """Defensive: a plan with no devices must stop, not spin.

        build_random_plan cannot return empty for a non-empty device list
        today, so this pins the guard rather than a reachable path. Without it
        the loop would keep re-rolling until the cycle ceiling.
        """
        import threading

        monkeypatch.setattr("group_fire.time.sleep", lambda s: None)
        monkeypatch.setattr("group_fire.time.monotonic", lambda: 0.0)

        calls = {"n": 0}
        real = group_fire_module.build_random_plan

        def sometimes_empty(devices: Any, defaults: Any) -> Any:
            calls["n"] += 1
            if calls["n"] > 1:
                return [], [], [], 0.0
            return real(devices, defaults)

        monkeypatch.setattr(group_fire_module, "build_random_plan", sometimes_empty)
        defaults = ActuationDefaults(
            device_count_range=[1, 1],
            spray_duration_range=[0.5, 0.5],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
        )
        result: dict[str, Any] = {}

        def run() -> None:
            result["execution"] = execute_plan(
                FakeController(), [_device("v1")], defaults,
                request_id="rid", event_type="detection", label="T",
                on_stuck=lambda d, e: None, deadline_sec=999999.0, rotate=True,
            )

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=20.0)
        assert not t.is_alive(), "an empty re-roll spun instead of stopping"
        assert len(result["execution"].actions) == 1


class TestForceOffStopsRotation:
    """Emergency off must survive the next cycle.

    It sends OFF to every device but changes none of the gates the rotation
    checks: not enabled, not armed, not the shutdown event. Before the latch,
    the next cycle turned the devices it had just switched off straight back
    on, so the panic button worked for a moment and then undid itself.
    """

    def test_bumping_the_latch_stops_the_rotation(self, monkeypatch: Any) -> None:
        from request_handler import ForceOffLatch

        clock = {"t": 0.0}
        monkeypatch.setattr("group_fire.time.monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            "group_fire.time.sleep", lambda s: clock.__setitem__("t", clock["t"] + s),
        )
        controller = FakeController()
        real = controller.activate_device
        latch = ForceOffLatch()
        started = latch.generation

        def timed(device: DeviceConfig, duration: float, **kw: Any) -> Any:
            clock["t"] += duration
            if len(controller.calls) == 1:
                latch.bump()  # operator hits emergency off mid-sequence
            return real(device, duration, **kw)

        controller.activate_device = timed  # type: ignore[method-assign]
        defaults = ActuationDefaults(
            device_count_range=[2, 2],
            spray_duration_range=[5.0, 5.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
        )
        execution = execute_plan(
            controller, [_device("v1"), _device("v2")], defaults,
            request_id="rid", event_type="detection", label="T",
            on_stuck=lambda d, e: None, deadline_sec=300.0, rotate=True,
            should_continue=lambda: latch.generation == started,
        )
        # The cycle in flight finishes; nothing new starts.
        assert len(execution.actions) == 2

    def test_latch_generation_only_moves_on_bump(self) -> None:
        from request_handler import ForceOffLatch

        latch = ForceOffLatch()
        g = latch.generation
        assert latch.generation == g
        latch.bump()
        assert latch.generation != g

    def test_force_off_handler_latches_before_sending_off(self) -> None:
        """Latching after the OFFs would leave a gap for the next cycle."""
        import queue as _queue

        from request_handler import ForceOffLatch, RequestHandler

        latch = ForceOffLatch()
        started = latch.generation
        handler = RequestHandler(
            {}, AtomicRef(None), AtomicRef(None),
            job_queue=_queue.Queue(), force_off_latch=latch,
        )
        # No controller, so the handler bails early; the latch must already
        # have moved by then.
        handler._handle_force_off(FakeRedisPub(), {"request_id": "r1"})
        assert latch.generation != started


class FakeRedisPub:
    def publish(self, channel: str, payload: str) -> None:
        pass


class TestAbortIsSticky:
    """Once revoked, a sequence stays stopped.

    should_continue can legitimately flip back to True: armed and
    deterrent.enabled are both re-settable while a sequence runs. The
    `aborted` flag makes the stop stick, so a re-arm landing in that gap
    cannot resume a sequence the operator just stopped.
    """

    @staticmethod
    def _defaults() -> ActuationDefaults:
        return ActuationDefaults(
            device_count_range=[2, 2],
            spray_duration_range=[5.0, 5.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
        )

    def test_a_re_arm_does_not_resume_a_stopped_sequence(self, monkeypatch: Any) -> None:
        clock = {"t": 0.0}
        monkeypatch.setattr("group_fire.time.monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            "group_fire.time.sleep", lambda s: clock.__setitem__("t", clock["t"] + s),
        )
        controller = FakeController()
        real = controller.activate_device

        def timed(device: DeviceConfig, duration: float, **kw: Any) -> Any:
            clock["t"] += duration
            return real(device, duration, **kw)

        controller.activate_device = timed  # type: ignore[method-assign]

        # False exactly once, then True forever: a disarm immediately undone.
        state = {"refused": False}

        def flapping() -> bool:
            if not state["refused"]:
                state["refused"] = True
                return False
            return True

        execution = execute_plan(
            controller, [_device("v1"), _device("v2")], self._defaults(),
            request_id="rid", event_type="detection", label="T",
            on_stuck=lambda d, e: None,
            deadline_sec=300.0, rotate=True, should_continue=flapping,
        )
        assert len(execution.actions) == 0, (
            "a re-arm resumed a sequence that had already been stopped"
        )


class TestAbortBeforeTheWait:
    """The gate runs before the inter-device wait, not only after it.

    The wait can be up to MAX_INTER_DELAY_SEC. Checking only afterwards left
    the worker parked for that long after the button was pressed, delaying the
    actuation record and the cooldown even though nothing was firing.
    """

    def test_stop_does_not_sit_through_the_wait(self, monkeypatch: Any) -> None:
        clock = {"t": 0.0}
        monkeypatch.setattr("group_fire.time.monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            "group_fire.time.sleep", lambda s: clock.__setitem__("t", clock["t"] + s),
        )
        controller = FakeController()
        real = controller.activate_device
        fired: list[str] = []

        def timed(device: DeviceConfig, duration: float, **kw: Any) -> Any:
            fired.append(device.name)
            clock["t"] += duration
            return real(device, duration, **kw)

        controller.activate_device = timed  # type: ignore[method-assign]
        defaults = ActuationDefaults(
            device_count_range=[2, 2],
            spray_duration_range=[1.0, 1.0],
            inter_device_delay_range=[30.0, 30.0],   # the maximum wait
            pre_delay_range=[0.0, 0.0],
        )
        execution = execute_plan(
            controller, [_device("v1"), _device("v2")], defaults,
            request_id="rid", event_type="detection", label="T",
            on_stuck=lambda d, e: None,
            deadline_sec=300.0, rotate=True,
            should_continue=lambda: len(fired) < 1,
        )
        # Device 1 fires (1s). Device 2's gate is consulted BEFORE its 30s
        # wait, so the sequence ends at t=1, not t=31.
        assert len(execution.actions) == 1
        assert execution.total_duration_sec == 1.0, (
            f"sat through the inter-device wait after the stop: "
            f"{execution.total_duration_sec}s"
        )

    def test_a_stop_at_the_firing_gate_is_also_sticky(self, monkeypatch: Any) -> None:
        """Both gates must set the flag, not just the first one.

        There are two gates per device: one before the inter-device wait and
        one immediately before firing. If only the first marks the sequence
        aborted, a stop landing on the second breaks the current cycle and the
        outer loop starts another one.
        """
        clock = {"t": 0.0}
        monkeypatch.setattr("group_fire.time.monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            "group_fire.time.sleep", lambda s: clock.__setitem__("t", clock["t"] + s),
        )
        controller = FakeController()
        real = controller.activate_device

        def timed(device: DeviceConfig, duration: float, **kw: Any) -> Any:
            clock["t"] += duration
            return real(device, duration, **kw)

        controller.activate_device = timed  # type: ignore[method-assign]

        # Refuse on the SECOND call only: gate one (pre-wait) passes, gate two
        # (pre-fire) refuses, everything after would allow it again.
        calls = {"n": 0}

        def refuse_second() -> bool:
            calls["n"] += 1
            return calls["n"] != 2

        defaults = ActuationDefaults(
            device_count_range=[2, 2],
            spray_duration_range=[5.0, 5.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
        )
        execution = execute_plan(
            controller, [_device("v1"), _device("v2")], defaults,
            request_id="rid", event_type="detection", label="T",
            on_stuck=lambda d, e: None,
            deadline_sec=300.0, rotate=True, should_continue=refuse_second,
        )
        assert len(execution.actions) == 0, (
            "a stop at the firing gate did not stick, the sequence restarted"
        )


class TestWaitsAreInterruptible:
    """A stop landing mid-wait must not sit out the rest of it.

    pre_delay and the inter-device gap are each bounded at 30s. Gating before
    a wait does not help if the button is pressed one second into it, so the
    waits are slept in slices and checked between them.
    """

    @staticmethod
    def _clock(monkeypatch: Any) -> dict:
        clock = {"t": 0.0}
        monkeypatch.setattr("group_fire.time.monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            "group_fire.time.sleep", lambda s: clock.__setitem__("t", clock["t"] + s),
        )
        return clock

    def test_a_stop_during_the_pre_delay_does_not_wait_it_out(self, monkeypatch: Any) -> None:
        clock = self._clock(monkeypatch)
        controller = FakeController()
        calls = {"n": 0}

        def revoke_partway() -> bool:
            calls["n"] += 1
            return calls["n"] <= 4          # allow ~1s of a 30s pre-delay

        execution = execute_plan(
            controller, [_device("v1")],
            ActuationDefaults(
                device_count_range=[1, 1],
                spray_duration_range=[5.0, 5.0],
                inter_device_delay_range=[0.0, 0.0],
                pre_delay_range=[30.0, 30.0],
            ),
            request_id="rid", event_type="detection", label="T",
            on_stuck=lambda d, e: None, deadline_sec=300.0, rotate=True,
            should_continue=revoke_partway,
        )
        assert controller.calls == [], "fired after authorisation was revoked"
        assert clock["t"] < 5.0, (
            f"sat through the rest of a 30s pre-delay: stopped at {clock['t']}s"
        )
        assert execution.aborted is True

    def test_a_stop_during_the_inter_device_wait_does_not_wait_it_out(self, monkeypatch: Any) -> None:
        clock = self._clock(monkeypatch)
        controller = FakeController()
        real = controller.activate_device

        def timed(device: DeviceConfig, duration: float, **kw: Any) -> Any:
            clock["t"] += duration
            return real(device, duration, **kw)

        controller.activate_device = timed  # type: ignore[method-assign]
        fired_at = {"t": None}
        calls = {"n": 0}

        def revoke_after_first_device() -> bool:
            calls["n"] += 1
            if controller.calls and fired_at["t"] is None:
                fired_at["t"] = clock["t"]
            # Allow everything up to and including the first device, then a
            # few slices into the 30s wait, then revoke.
            return not (fired_at["t"] is not None and clock["t"] >= fired_at["t"] + 1.0)

        execute_plan(
            controller, [_device("v1"), _device("v2")],
            ActuationDefaults(
                device_count_range=[2, 2],
                spray_duration_range=[1.0, 1.0],
                inter_device_delay_range=[30.0, 30.0],
                pre_delay_range=[0.0, 0.0],
            ),
            request_id="rid", event_type="detection", label="T",
            on_stuck=lambda d, e: None, deadline_sec=300.0, rotate=True,
            should_continue=revoke_after_first_device,
        )
        assert len(controller.calls) == 1
        assert clock["t"] < 10.0, (
            f"sat through the rest of a 30s wait: stopped at {clock['t']}s"
        )


class TestAbortedIsReported:
    """The caller has to tell an operator which of the two happened."""

    @staticmethod
    def _defaults() -> ActuationDefaults:
        return ActuationDefaults(
            device_count_range=[1, 1],
            spray_duration_range=[5.0, 5.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
        )

    def _run(self, monkeypatch: Any, should_continue: Any, deadline: float) -> Any:
        clock = {"t": 0.0}
        monkeypatch.setattr("group_fire.time.monotonic", lambda: clock["t"])
        monkeypatch.setattr(
            "group_fire.time.sleep", lambda s: clock.__setitem__("t", clock["t"] + s),
        )
        controller = FakeController()
        real = controller.activate_device

        def timed(device: DeviceConfig, duration: float, **kw: Any) -> Any:
            clock["t"] += duration
            return real(device, duration, **kw)

        controller.activate_device = timed  # type: ignore[method-assign]
        return execute_plan(
            controller, [_device("v1")], self._defaults(),
            request_id="rid", event_type="detection", label="T",
            on_stuck=lambda d, e: None, deadline_sec=deadline, rotate=True,
            should_continue=should_continue,
        )

    def test_revoked_sets_aborted(self, monkeypatch: Any) -> None:
        execution = self._run(monkeypatch, lambda: False, 300.0)
        assert execution.aborted is True

    def test_an_elapsed_window_does_not_set_aborted(self, monkeypatch: Any) -> None:
        execution = self._run(monkeypatch, lambda: True, 6.0)
        assert execution.aborted is False
        assert execution.actions, "nothing fired, so the window was not the cause"
