"""Tests for the OFF-retry, watchdog, and force-off behaviour of the
TuyaCloudController. The Tuya Cloud client is mocked — we're testing the
state machine around it, not the network."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from actuation_models import DeviceConfig
from cloud_controller import ActivationResult, TuyaCloudController


@pytest.fixture
def device() -> DeviceConfig:
    return DeviceConfig(
        name="pond-sprinkler",
        device_id="bf123abc",
        type="sprinkler",
        enabled=True,
    )


@pytest.fixture
def controller() -> TuyaCloudController:
    """Construct a controller with the tinytuya.Cloud client mocked out."""
    with patch("cloud_controller.tinytuya.Cloud") as cloud_cls:
        cloud_cls.return_value = MagicMock()
        ctrl = TuyaCloudController(api_key="x", api_secret="y")
    return ctrl


def _success_response() -> dict[str, Any]:
    return {"success": True, "result": {}}


def _failure_response() -> dict[str, Any]:
    return {"success": False, "msg": "device offline"}


class TestActivationResult:
    def test_success_property_requires_both(self) -> None:
        r = ActivationResult(
            on_success=True, off_success=True, error=None,
            on_ack_ms=10.0, off_attempts=1,
        )
        assert r.success is True
        assert r.stuck is False

    def test_off_failure_is_stuck(self) -> None:
        r = ActivationResult(
            on_success=True, off_success=False, error="OFF_FAILED:x",
            on_ack_ms=10.0, off_attempts=4,
        )
        assert r.success is False
        assert r.stuck is True

    def test_on_failure_is_not_stuck(self) -> None:
        # If ON never succeeded, the device isn't physically on — not stuck.
        r = ActivationResult(
            on_success=False, off_success=None, error="ON failed",
            on_ack_ms=10.0, off_attempts=0,
        )
        assert r.success is False
        assert r.stuck is False


class TestActivateDevice:
    def test_happy_path(
        self, controller: TuyaCloudController, device: DeviceConfig,
    ) -> None:
        controller._cloud.sendcommand = MagicMock(return_value=_success_response())
        result = controller.activate_device(device, 0.5, request_id="rid1")
        assert result.success is True
        assert result.stuck is False
        assert result.off_attempts == 1
        # Sent ON then OFF — exactly two cloud calls on the happy path.
        assert controller._cloud.sendcommand.call_count == 2

    def test_clamps_oversized_duration(
        self, controller: TuyaCloudController, device: DeviceConfig,
    ) -> None:
        controller._cloud.sendcommand = MagicMock(return_value=_success_response())
        # Patch sleep so the test doesn't actually wait 60s.
        with patch("cloud_controller.time.sleep") as sleep_mock:
            controller.activate_device(device, 86400.0, request_id="rid")
            assert sleep_mock.called
            # The duration argument to time.sleep is the first positional.
            slept_for = sleep_mock.call_args.args[0]
            from deterrent_safety import MAX_ACTUATION_SEC
            assert slept_for <= MAX_ACTUATION_SEC

    def test_on_failure_returns_not_stuck(
        self, controller: TuyaCloudController, device: DeviceConfig,
    ) -> None:
        controller._cloud.sendcommand = MagicMock(return_value=_failure_response())
        result = controller.activate_device(device, 0.5, request_id="rid")
        assert result.on_success is False
        assert result.off_success is None
        assert result.stuck is False
        assert result.error is not None

    def test_off_retried_on_failure(
        self, controller: TuyaCloudController, device: DeviceConfig,
    ) -> None:
        # ON succeeds; OFF fails once then succeeds.
        responses = [
            _success_response(),    # ON
            _failure_response(),    # OFF #1
            _success_response(),    # OFF #2 (retry)
        ]
        controller._cloud.sendcommand = MagicMock(side_effect=responses)
        # Skip the actual backoff sleep so test runs fast.
        with patch("cloud_controller.time.sleep"):
            result = controller.activate_device(device, 0.5, request_id="rid")
        assert result.success is True
        assert result.off_attempts == 2

    def test_off_exhausts_retries(
        self, controller: TuyaCloudController, device: DeviceConfig,
    ) -> None:
        # ON succeeds; OFF fails forever.
        def responses() -> Any:
            yield _success_response()  # ON
            while True:
                yield _failure_response()
        controller._cloud.sendcommand = MagicMock(side_effect=responses())
        with patch("cloud_controller.time.sleep"):
            result = controller.activate_device(device, 0.5, request_id="rid")
        assert result.on_success is True
        assert result.off_success is False
        assert result.stuck is True
        assert result.error is not None
        assert "OFF_FAILED" in result.error
        # 1 ON + 4 OFF (1 initial + 3 retries) = 5
        assert controller._cloud.sendcommand.call_count == 5

    def test_off_handles_exception(
        self, controller: TuyaCloudController, device: DeviceConfig,
    ) -> None:
        # ON succeeds; OFF raises every time.
        def side_effect(*_args: Any, **_kw: Any) -> Any:
            if not getattr(side_effect, "_did_on", False):
                side_effect._did_on = True  # type: ignore[attr-defined]
                return _success_response()
            raise RuntimeError("network blip")
        controller._cloud.sendcommand = MagicMock(side_effect=side_effect)
        with patch("cloud_controller.time.sleep"):
            result = controller.activate_device(device, 0.5, request_id="rid")
        assert result.stuck is True
        assert result.error is not None
        assert "OFF_FAILED" in result.error


class TestForceOff:
    def test_succeeds_first_try(
        self, controller: TuyaCloudController, device: DeviceConfig,
    ) -> None:
        controller._cloud.sendcommand = MagicMock(return_value=_success_response())
        ok, err = controller.force_off(device, request_id="rid-emergency")
        assert ok is True
        assert err is None

    def test_returns_failure_after_retries(
        self, controller: TuyaCloudController, device: DeviceConfig,
    ) -> None:
        controller._cloud.sendcommand = MagicMock(return_value=_failure_response())
        with patch("cloud_controller.time.sleep"):
            ok, err = controller.force_off(device, request_id="rid")
        assert ok is False
        assert err is not None
        assert "OFF_FAILED" in err


class TestBusyTracking:
    def test_busy_during_activation(
        self, controller: TuyaCloudController, device: DeviceConfig,
    ) -> None:
        sleep_observed: list[bool] = []

        def fake_sleep(_d: float) -> None:
            # Mid-activation: the busy flag must be set so the reconcile
            # loop won't race a legitimate in-flight actuation.
            sleep_observed.append(controller.is_device_busy(device.device_id))

        controller._cloud.sendcommand = MagicMock(return_value=_success_response())
        with patch("cloud_controller.time.sleep", side_effect=fake_sleep):
            controller.activate_device(device, 0.5, request_id="rid")
        assert sleep_observed and sleep_observed[0] is True
        # Cleared after activation finishes.
        assert controller.is_device_busy(device.device_id) is False


class TestIsSwitchedOn:
    def test_returns_true_when_dp_on(
        self, controller: TuyaCloudController, device: DeviceConfig,
    ) -> None:
        controller._cloud.getstatus = MagicMock(return_value={
            "success": True,
            "result": [{"code": "switch_1", "value": True}],
        })
        assert controller.is_switched_on(device) is True

    def test_returns_false_when_dp_off(
        self, controller: TuyaCloudController, device: DeviceConfig,
    ) -> None:
        controller._cloud.getstatus = MagicMock(return_value={
            "success": True,
            "result": [{"code": "switch_1", "value": False}],
        })
        assert controller.is_switched_on(device) is False

    def test_returns_none_when_unreachable(
        self, controller: TuyaCloudController, device: DeviceConfig,
    ) -> None:
        controller._cloud.getstatus = MagicMock(return_value={"success": False})
        assert controller.is_switched_on(device) is None


class TestWatchdog:
    """The watchdog timer is the safety net of last resort. Even if every
    other layer is bypassed, the timer fires force-OFF after MAX_ACTUATION_SEC."""

    def test_watchdog_cancelled_on_clean_off(
        self, controller: TuyaCloudController, device: DeviceConfig,
    ) -> None:
        timer_mock = MagicMock()
        controller._cloud.sendcommand = MagicMock(return_value=_success_response())
        with patch("cloud_controller.threading.Timer", return_value=timer_mock):
            controller.activate_device(device, 0.5, request_id="rid")
        timer_mock.start.assert_called_once()
        timer_mock.cancel.assert_called_once()

    def test_watchdog_fires_after_max_duration(
        self, controller: TuyaCloudController, device: DeviceConfig,
    ) -> None:
        # Simulate the watchdog actually invoking _send_off_with_retry.
        controller._cloud.sendcommand = MagicMock(return_value=_success_response())
        controller._watchdog_fire(device, "switch_1", request_id="watchdog-rid")
        # One OFF call was made.
        assert controller._cloud.sendcommand.call_count == 1
        sent_cmd = controller._cloud.sendcommand.call_args.args[1]
        assert sent_cmd["commands"][0]["value"] is False


def test_watchdog_timer_uses_max_actuation_sec(device: DeviceConfig) -> None:
    """The Timer interval passed to threading.Timer must be MAX_ACTUATION_SEC."""
    from deterrent_safety import MAX_ACTUATION_SEC
    with patch("cloud_controller.tinytuya.Cloud"):
        ctrl = TuyaCloudController(api_key="x", api_secret="y")
    ctrl._cloud.sendcommand = MagicMock(return_value=_success_response())
    captured: dict[str, Any] = {}

    def fake_timer(interval: float, target: Any, args: Any = ()) -> Any:
        captured["interval"] = interval
        m = MagicMock()
        return m

    with patch("cloud_controller.threading.Timer", side_effect=fake_timer):
        with patch("cloud_controller.time.sleep"):
            ctrl.activate_device(device, 0.5, request_id="rid")
    assert captured["interval"] == MAX_ACTUATION_SEC
