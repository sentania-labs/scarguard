"""Tests for the randomisation engine."""

from __future__ import annotations

from actuation_models import ActuationDefaults, DeviceConfig
from randomizer import build_random_plan


def _device(name: str, dev_type: str = "sprinkler") -> DeviceConfig:
    return DeviceConfig(name=name, device_id=f"id-{name}", type=dev_type)


class TestBuildRandomPlan:
    def test_empty_devices_returns_empty(self) -> None:
        selected, durations, delays, pre = build_random_plan([], ActuationDefaults())
        assert selected == []
        assert durations == []
        assert delays == []
        assert pre == 0.0

    def test_single_device(self) -> None:
        devices = [_device("v1")]
        defaults = ActuationDefaults(device_count_range=[1, 1])
        selected, durations, delays, pre = build_random_plan(devices, defaults)
        assert len(selected) == 1
        assert len(durations) == 1
        assert len(delays) == 1
        assert delays[0] == 0.0  # first device has no inter-delay

    def test_respects_count_range(self) -> None:
        devices = [_device(f"v{i}") for i in range(6)]
        defaults = ActuationDefaults(device_count_range=[2, 3])
        for _ in range(50):
            selected, *_ = build_random_plan(devices, defaults)
            assert 2 <= len(selected) <= 3

    def test_count_capped_by_available_devices(self) -> None:
        devices = [_device("v1"), _device("v2")]
        defaults = ActuationDefaults(device_count_range=[1, 10])
        for _ in range(20):
            selected, *_ = build_random_plan(devices, defaults)
            assert len(selected) <= 2

    def test_durations_within_range(self) -> None:
        devices = [_device(f"v{i}") for i in range(4)]
        defaults = ActuationDefaults(spray_duration_range=[2.0, 5.0])
        for _ in range(30):
            _, durations, *_ = build_random_plan(devices, defaults)
            for d in durations:
                assert 2.0 <= d <= 5.0

    def test_pre_delay_within_range(self) -> None:
        devices = [_device("v1")]
        defaults = ActuationDefaults(pre_delay_range=[1.0, 3.0])
        for _ in range(30):
            *_, pre = build_random_plan(devices, defaults)
            assert 1.0 <= pre <= 3.0

    def test_inter_delays_within_range(self) -> None:
        devices = [_device(f"v{i}") for i in range(4)]
        defaults = ActuationDefaults(
            device_count_range=[4, 4],
            inter_device_delay_range=[0.5, 2.0],
        )
        _, _, delays, _ = build_random_plan(devices, defaults)
        assert delays[0] == 0.0  # first is always 0
        for d in delays[1:]:
            assert 0.5 <= d <= 2.0

    def test_selected_are_from_input(self) -> None:
        devices = [_device(f"v{i}") for i in range(5)]
        selected, *_ = build_random_plan(devices, ActuationDefaults())
        for d in selected:
            assert d in devices

    def test_no_duplicate_devices(self) -> None:
        devices = [_device(f"v{i}") for i in range(5)]
        for _ in range(30):
            selected, *_ = build_random_plan(devices, ActuationDefaults())
            ids = [d.device_id for d in selected]
            assert len(ids) == len(set(ids))
