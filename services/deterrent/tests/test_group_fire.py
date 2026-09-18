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
from deterrent_safety import MAX_ACTUATION_SEC
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
