"""Tests for the shared group-firing sequence.

Both the detection path and the admin group test-fire drive hardware through
``execute_plan``, so the safety properties asserted here (every activation is
clamped, every activation goes through the controller, a stuck device is always
reported) cover both callers at once.
"""

from __future__ import annotations

from typing import Any

from actuation_models import ActuationDefaults, DeterrentGroup, DeviceConfig
from cloud_controller import ActivationResult
from deterrent_safety import (
    MAX_ACTUATION_SEC,
    MAX_INTER_DELAY_SEC,
    MAX_PRE_DELAY_SEC,
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

        assert 2.5 in slept, "pre_delay was never slept"

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

        # No pre-delay, so the very first thing that happens is a firing.
        assert trace[0][0] == "fire", f"device 0 waited before firing: {trace[:2]}"
        # Thereafter strictly alternating: sleep, fire, sleep, fire.
        assert [kind for kind, _ in trace] == [
            "fire", "sleep", "fire", "sleep", "fire",
        ]
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
        # Cycle 1 ends at t=10 (under 12, so cycle 2 starts); its first device
        # runs 10->15, then the window has closed.
        assert len(execution.actions) == 3
        assert execution.total_duration_sec == 15.0

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
