"""Tests for the OFF-retry, watchdog, and force-off behaviour of the
TuyaCloudController. The Tuya Cloud client is mocked - we're testing the
state machine around it, not the network."""

from __future__ import annotations

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
        # If ON never succeeded, the device isn't physically on - not stuck.
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
        # Sent ON then OFF - exactly two cloud calls on the happy path.
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


class TestEmergencyOffOrdering:
    def test_cancelled_gate_never_sends_on(self, controller: TuyaCloudController, device: DeviceConfig) -> None:
        from request_handler import ForceOffLatch

        latch = ForceOffLatch()
        generation = latch.generation
        latch.bump()
        result = controller.activate_device(
            device, .5, should_continue=lambda: latch.generation == generation,
        )
        assert not result.on_success
        assert result.off_attempts == 0
        controller._cloud.sendcommand.assert_not_called()
        assert not controller.is_device_busy(device.device_id)

    def test_off_completed_between_outer_gate_and_activation(self, controller: TuyaCloudController, device: DeviceConfig) -> None:
        from request_handler import ForceOffLatch

        latch = ForceOffLatch()
        generation = latch.generation
        controller._cloud.sendcommand.return_value = _success_response()
        # The group worker passed its gate; emergency off now completes.
        assert latch.generation == generation
        latch.bump()
        assert controller.force_off(device)[0]
        result = controller.activate_device(
            device, .5, should_continue=lambda: latch.generation == generation,
        )
        assert not result.on_success
        assert [call.args[1]['commands'][0]['value'] for call in controller._cloud.sendcommand.call_args_list] == [False]

    @pytest.mark.parametrize('rebuilt', [False, True])
    def test_off_waits_for_admitted_on_then_no_stale_on(self, controller: TuyaCloudController, device: DeviceConfig, rebuilt: bool) -> None:
        import threading

        from request_handler import ForceOffLatch

        off_controller = controller
        if rebuilt:
            with patch('cloud_controller.tinytuya.Cloud'):
                off_controller = TuyaCloudController('new', 'credentials')
        latch = ForceOffLatch()
        generation = latch.generation
        on_entered = threading.Event()
        release_on = threading.Event()
        off_requested = threading.Event()
        off_done = threading.Event()
        calls: list[bool] = []
        failures: list[BaseException] = []

        def send(_id: str, command: dict) -> dict:
            value = command['commands'][0]['value']
            if value:
                on_entered.set()
                assert release_on.wait(3)
            calls.append(value)
            return _success_response()

        def activate() -> None:
            try:
                controller.activate_device(device, .5, should_continue=lambda: latch.generation == generation)
            except BaseException as exc:
                failures.append(exc)

        def emergency_off() -> None:
            try:
                latch.bump()
                off_requested.set()
                assert off_controller.force_off(device)[0]
                off_done.set()
            except BaseException as exc:
                failures.append(exc)

        controller._cloud.sendcommand.side_effect = send
        off_controller._cloud.sendcommand.side_effect = send
        worker = threading.Thread(target=activate)
        emergency = threading.Thread(target=emergency_off)
        worker.start()
        try:
            assert on_entered.wait(3)
            emergency.start()
            assert off_requested.wait(3)
            assert not off_done.is_set()
        finally:
            release_on.set()
            worker.join(4)
            if emergency.ident is not None:
                emergency.join(4)
        assert not worker.is_alive() and not emergency.is_alive()
        assert not failures
        assert off_done.is_set()
        assert calls[0] is True
        assert all(value is False for value in calls[1:])
        stale = controller.activate_device(device, .5, should_continue=lambda: latch.generation == generation)
        assert not stale.on_success
        assert calls.count(True) == 1

    def test_group_propagates_last_moment_cancellation(self, controller: TuyaCloudController, device: DeviceConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        import group_fire
        from actuation_models import ActuationDefaults
        from request_handler import ForceOffLatch

        latch = ForceOffLatch()
        generation = latch.generation
        controller._cloud.sendcommand.return_value = _success_response()
        real_clamp = group_fire.clamp_duration

        def cancel_at_clamp(*args, **kwargs) -> float:
            latch.bump()
            assert controller.force_off(device)[0]
            return real_clamp(*args, **kwargs)

        monkeypatch.setattr(group_fire, 'clamp_duration', cancel_at_clamp)
        execution = group_fire.execute_plan(
            controller, [device], ActuationDefaults(pre_delay_range=[0, 0], inter_device_delay_range=[0, 0], device_count_range=[1, 1]),
            request_id='race', event_type='detection', label='race', on_stuck=lambda *args: None,
            should_continue=lambda: latch.generation == generation,
        )
        assert execution.aborted
        assert not execution.actions
        assert controller._cloud.sendcommand.call_count == 1
        assert controller._cloud.sendcommand.call_args.args[1]['commands'][0]['value'] is False

    def test_single_test_fire_propagates_last_moment_cancellation(self, controller: TuyaCloudController, device: DeviceConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        import main
        from actuation_models import ActuationConfig
        from atomic_ref import AtomicRef
        from request_handler import ForceOffLatch

        latch = ForceOffLatch()
        generation = latch.generation
        controller._cloud.sendcommand.return_value = _success_response()

        def cancel_before_call(*args, **kwargs) -> None:
            if str(args[0]).startswith('Test-fire:'):
                latch.bump()
                assert controller.force_off(device)[0]

        monkeypatch.setattr(main.logger, 'info', cancel_before_call)
        monkeypatch.setattr(main.actuation_db, 'insert_event', lambda *args: None)
        main._run_test_fire(
            {'device_id': device.device_id, 'duration_sec': .5, 'request_id': 'race',
             'result_channel': 'fixture', 'force_off_gen': generation},
            AtomicRef(ActuationConfig(devices=[device])), AtomicRef(controller), [MagicMock()], {}, latch,
        )
        assert controller._cloud.sendcommand.call_count == 1
        assert controller._cloud.sendcommand.call_args.args[1]['commands'][0]['value'] is False
