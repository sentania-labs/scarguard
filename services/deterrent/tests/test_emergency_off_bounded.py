"""SG-01 regression coverage for bounded emergency OFF behaviour.

These tests exercise the real cloud controller and lifecycle sweep helpers.
The fake cloud deliberately never returns from selected calls; no device or
network access is involved.
"""

from __future__ import annotations

import json
import signal
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

from actuation_models import ActuationConfig, DeviceConfig
from atomic_ref import AtomicRef
from cloud_controller import TuyaCloudController
from deterrent_safety import CLOUD_CALL_TIMEOUT_SEC, EMERGENCY_OFF_BOUND_SEC
from main import _force_off_sweep, _make_shutdown_handler
from request_handler import RequestHandler


def _device() -> DeviceConfig:
    return DeviceConfig(
        name="pond-sprinkler",
        device_id="sg01-device",
        type="sprinkler",
    )


def _controller() -> TuyaCloudController:
    with patch("cloud_controller.tinytuya.Cloud") as cloud_cls:
        cloud_cls.return_value = MagicMock()
        return TuyaCloudController(api_key="test", api_secret="test")


def _is_on(command: dict[str, Any]) -> bool:
    return bool(command["commands"][0]["value"])


def test_stalled_status_poll_cannot_block_emergency_off() -> None:
    controller = _controller()
    device = _device()
    status_started = threading.Event()
    never_release = threading.Event()
    off_sent = threading.Event()

    def stalled_status(_device_id: str) -> dict[str, Any]:
        status_started.set()
        never_release.wait()
        raise AssertionError("unreachable")

    def command(_device_id: str, body: dict[str, Any]) -> dict[str, Any]:
        assert not _is_on(body)
        off_sent.set()
        return {"success": True}

    controller._cloud.getstatus.side_effect = stalled_status
    controller._cloud.sendcommand.side_effect = command
    poll = threading.Thread(target=controller.get_device_status, args=(device.device_id,))
    poll.start()
    assert status_started.wait(1)

    started = time.monotonic()
    ok, error = controller.force_off(device, request_id="emergency")
    elapsed = time.monotonic() - started

    assert ok, error
    assert off_sent.is_set()
    assert elapsed < EMERGENCY_OFF_BOUND_SEC
    # The fake remained stalled throughout the emergency operation. Release
    # it only to avoid contaminating later tests via the process-wide lane.
    never_release.set()
    _wait_for_workers_to_exit("tuya-status")
    poll.join(CLOUD_CALL_TIMEOUT_SEC + 1)
    assert not poll.is_alive(), "bounded status wrapper did not return"


def _wait_for_workers_to_exit(name: str) -> None:
    deadline = time.monotonic() + 1
    while (
        any(thread.name == name for thread in threading.enumerate())
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)


def test_stalled_on_starts_deadline_first_and_attempts_off() -> None:
    controller = _controller()
    device = _device()
    on_started = threading.Event()
    never_release = threading.Event()
    off_sent = threading.Event()
    call_times: list[tuple[bool, float]] = []
    on_attempt_times: list[float] = []
    timers: list[MagicMock] = []

    def command(_device_id: str, body: dict[str, Any]) -> dict[str, Any]:
        value = _is_on(body)
        if value:
            on_attempt_times.append(time.monotonic())
            on_started.set()
            never_release.wait()
            call_times.append((value, time.monotonic()))
            return {"success": True}
        call_times.append((value, time.monotonic()))
        off_sent.set()
        return {"success": True}

    controller._cloud.sendcommand.side_effect = command
    started = time.monotonic()
    def timer(_delay: float, target: Any, args: tuple[Any, ...]) -> MagicMock:
        captured = MagicMock()
        captured.fire = lambda: target(*args)
        timers.append(captured)
        return captured

    with patch("cloud_controller.threading.Timer", side_effect=timer):
        result = controller.activate_device(device, 0.5, request_id="ambiguous-on")
    elapsed = time.monotonic() - started

    assert on_started.is_set()
    assert result.on_success is False
    assert result.off_success is True
    assert result.off_attempts >= 1
    assert off_sent.is_set(), "ambiguous ON failure did not trigger OFF"
    assert elapsed < EMERGENCY_OFF_BOUND_SEC
    assert controller.last_off_deadline_started_at is not None
    assert controller.last_off_deadline_started_at <= on_attempt_times[0]
    assert not timers[0].cancel.called, "ambiguous ON disarmed the OFF deadline"

    # A timed-out HTTP worker can return after the compensating OFF. The
    # completion guard must issue OFF after that late ON, independently of
    # the still-armed deadline.
    never_release.set()
    _wait_for_workers_to_exit("tuya-on")
    deadline = time.monotonic() + 1
    while len(call_times) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [value for value, _when in call_times] == [False, True, False]
    timers[0].fire()
    assert [value for value, _when in call_times] == [False, True, False, False]


def test_startup_and_sigterm_sweeps_run_even_when_disabled() -> None:
    device = _device().model_copy(update={"enabled": False})
    config_ref = AtomicRef(ActuationConfig(enabled=False, devices=[device]))
    controller = MagicMock()
    controller.force_off.return_value = (True, None)
    controller_ref: AtomicRef[TuyaCloudController | None] = AtomicRef(controller)

    _force_off_sweep(controller_ref, config_ref, reason="startup")

    shutdown = threading.Event()
    queue_put = MagicMock()
    handler = _make_shutdown_handler(
        shutdown, queue_put, controller_ref, config_ref,
    )
    handler(signal.SIGTERM, None)

    assert shutdown.is_set()
    assert queue_put.call_count == 1
    assert controller.force_off.call_count == 2
    assert [call.kwargs["request_id"].split("-")[0]
            for call in controller.force_off.call_args_list] == ["startup", "shutdown"]


def test_status_handler_surfaces_unknown_state_warning() -> None:
    device = _device()
    controller = MagicMock()
    controller.get_device_status.return_value = None
    client = MagicMock()
    handler = RequestHandler(
        {}, AtomicRef(ActuationConfig(devices=[device])), AtomicRef(controller),
    )

    handler._handle_status_request(client, {"request_id": "unknown"})

    payload = json.loads(client.publish.call_args.args[1])
    assert payload["devices"][0]["online"] is False
    assert "STATUS UNKNOWN" in payload["devices"][0]["name"]
    assert "not verified" in payload["devices"][0]["warning"]


def test_stalled_off_attempt_cannot_poison_retry_lane() -> None:
    controller = _controller()
    device = _device()
    first_started = threading.Event()
    never_release = threading.Event()
    attempts = 0

    def command(_device_id: str, body: dict[str, Any]) -> dict[str, Any]:
        nonlocal attempts
        assert not _is_on(body)
        attempts += 1
        if attempts == 1:
            first_started.set()
            never_release.wait()
        return {"success": True}

    controller._cloud.sendcommand.side_effect = command
    with patch("cloud_controller.OFF_RETRY_BACKOFF_SEC", (0.0,)):
        ok, error = controller.force_off(device, request_id="retry-after-stall")

    assert first_started.is_set()
    assert ok, error
    assert attempts == 2
    never_release.set()
    _wait_for_workers_to_exit("tuya-off")


def test_repeated_calls_behind_stalled_status_do_not_leak_waiter_threads() -> None:
    controller = _controller()
    never_release = threading.Event()
    entered = threading.Event()

    def status(_device_id: str) -> dict[str, Any]:
        entered.set()
        never_release.wait()
        return {"success": False}

    controller._cloud.getstatus.side_effect = status
    with patch("cloud_controller.CLOUD_CALL_TIMEOUT_SEC", 0.05):
        assert controller.get_device_status("stalled") is None
        assert entered.is_set()
        stalled_workers = sum(
            thread.name == "tuya-status" for thread in threading.enumerate()
        )
        for _ in range(5):
            assert controller.get_device_status("queued") is None
        assert sum(
            thread.name == "tuya-status" for thread in threading.enumerate()
        ) == stalled_workers

    never_release.set()
    _wait_for_workers_to_exit("tuya-status")


def test_ambiguous_on_and_failed_off_is_reported_stuck() -> None:
    controller = _controller()
    device = _device()
    never_release = threading.Event()

    def command(_device_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if _is_on(body):
            never_release.wait()
        return {"success": False, "msg": "offline"}

    controller._cloud.sendcommand.side_effect = command
    with (
        patch("cloud_controller.CLOUD_CALL_TIMEOUT_SEC", 0.05),
        patch("cloud_controller.OFF_RETRY_BACKOFF_SEC", ()),
        patch("cloud_controller.threading.Timer", return_value=MagicMock()),
    ):
        result = controller.activate_device(device, 0.5, request_id="unsafe-unknown")

    assert result.on_success is False
    assert result.off_success is False
    assert result.stuck is True
    never_release.set()
    _wait_for_workers_to_exit("tuya-on")


def test_cancellation_while_waiting_for_on_admission_prevents_on() -> None:
    controller = _controller()
    allowed = threading.Event()
    allowed.set()
    checked = threading.Event()
    values: list[bool] = []

    def authorised() -> bool:
        checked.set()
        return allowed.is_set()

    def command(_id: str, body: dict[str, Any]) -> dict[str, Any]:
        values.append(_is_on(body))
        return {"success": True}

    controller._cloud.sendcommand.side_effect = command
    controller._lock.acquire()
    worker = threading.Thread(target=controller.activate_device,
                              args=(_device(), 0.5),
                              kwargs={"should_continue": authorised})
    try:
        worker.start()
        assert checked.wait(1)
        allowed.clear()
        assert controller.force_off(_device())[0]
    finally:
        controller._lock.release()
        worker.join(3)
    assert not worker.is_alive()
    assert True not in values, "cancelled ON was admitted after emergency OFF"


def test_on_returning_after_watchdog_is_compensated_without_reconciliation() -> None:
    controller = _controller()
    release = threading.Event()
    values: list[bool] = []

    def command(_id: str, body: dict[str, Any]) -> dict[str, Any]:
        on = _is_on(body)
        if on:
            release.wait()
        values.append(on)
        return {"success": True}

    controller._cloud.sendcommand.side_effect = command
    try:
        with patch("cloud_controller.CLOUD_CALL_TIMEOUT_SEC", 0.03), \
             patch("cloud_controller.MAX_ACTUATION_SEC", 0.1):
            result = controller.activate_device(_device(), 0.5)
            assert result.off_success
            deadline = time.monotonic() + 1
            while len(values) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert values == [False, False], "watchdog must fire before late ON"
            release.set()
            _wait_for_workers_to_exit("tuya-on")
            assert values == [False, False, True, False]
    finally:
        release.set()
        _wait_for_workers_to_exit("tuya-on")


def test_off_lock_admission_consumes_attempt_budget() -> None:
    controller = _controller()
    lock = controller._off_lock_for(_device().device_id)
    lock.acquire()
    worker = threading.Thread(target=controller.force_off, args=(_device(),))
    try:
        with patch("cloud_controller.CLOUD_CALL_TIMEOUT_SEC", 0.03), \
             patch("cloud_controller.OFF_RETRY_BACKOFF_SEC", ()):
            worker.start()
            worker.join(0.3)
            assert not worker.is_alive(), "OFF waited indefinitely for admission"
    finally:
        lock.release()
        worker.join(3)


def test_transient_initialization_recovers_without_config_change() -> None:
    from main import _controller_recovery_loop, build_controller

    release = threading.Event()
    config = ActuationConfig(tuya={"api_key": "test", "api_secret": "test"})
    shutdown = threading.Event()
    recovered = threading.Event()
    calls = 0

    def factory(**_kwargs: Any) -> MagicMock:
        nonlocal calls
        calls += 1
        if calls == 1:
            release.wait()
        return MagicMock()

    ref: AtomicRef[TuyaCloudController | None] = AtomicRef(None)

    def retry() -> None:
        controller = build_controller(config)
        ref.set(controller)
        if controller is not None:
            recovered.set()
            shutdown.set()

    with patch("cloud_controller.tinytuya.Cloud", side_effect=factory), \
         patch("cloud_controller.CLOUD_CALL_TIMEOUT_SEC", 0.03):
        assert build_controller(config) is None
        release.set()
        _wait_for_workers_to_exit("tuya-init")
        worker = threading.Thread(target=_controller_recovery_loop,
                                  args=(ref, shutdown, retry),
                                  kwargs={"retry_sec": 0.01})
        worker.start()
        try:
            assert recovered.wait(1)
            assert ref.get() is not None
            assert calls == 2
        finally:
            shutdown.set()
            worker.join(1)


def test_cancellation_during_on_ack_skips_duration_wait() -> None:
    controller = _controller()
    allowed = threading.Event()
    allowed.set()
    values: list[bool] = []

    def command(_id: str, body: dict[str, Any]) -> dict[str, Any]:
        values.append(_is_on(body))
        if _is_on(body):
            allowed.clear()
        return {"success": True}

    controller._cloud.sendcommand.side_effect = command
    started = time.monotonic()
    result = controller.activate_device(_device(), 10, should_continue=allowed.is_set)
    assert time.monotonic() - started < 0.5
    assert result.off_success
    assert values == [True, False, False]


def test_main_recovers_and_sweeps_with_reconciliation_disabled() -> None:
    import main

    device = _device()
    cfg = {"deterrent": {"enabled": False, "reconcile_interval_sec": 0,
                         "tuya": {"api_key": "test", "api_secret": "test"},
                         "devices": [device.model_dump()]}}
    cloud = MagicMock()
    cloud.sendcommand.return_value = {"success": True}
    release = threading.Event()
    recovery_swept = threading.Event()
    monitoring_ready = threading.Event()
    calls = 0
    real_sweep = main._force_off_sweep
    real_recovery = main._controller_recovery_loop

    def factory(**_kwargs: Any) -> MagicMock:
        nonlocal calls
        calls += 1
        if calls == 1:
            release.wait()
        return cloud

    def sweep(*args: Any, **kwargs: Any) -> dict[str, bool]:
        result = real_sweep(*args, **kwargs)
        if kwargs["reason"] == "startup":
            release.set()
        if kwargs["reason"] == "recovery":
            recovery_swept.set()
        return result

    def subscribe(_cfg: Any, _queue: Any, _shutdown: Any) -> None:
        assert recovery_swept.wait(2), "main did not recover without a config edit"
        assert monitoring_ready.wait(2), "recovery did not restore battery monitoring"
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    try:
        with (
            patch("cloud_controller.tinytuya.Cloud", side_effect=factory),
            patch("cloud_controller.CLOUD_CALL_TIMEOUT_SEC", 0.03),
            patch.object(main, "load_config", return_value=cfg),
            patch.object(main, "start_heartbeat"),
            patch.object(main.actuation_db, "init_db"),
            patch.object(main, "ConfigWatcher"),
            patch.object(main, "BatteryMonitor") as battery,
            patch.object(main, "RequestHandler"),
            patch.object(main, "_metrics_publisher"),
            patch.object(main, "_force_off_sweep", side_effect=sweep) as sweeps,
            patch.object(main, "subscribe_loop", side_effect=subscribe),
            patch.object(main, "_controller_recovery_loop",
                         side_effect=lambda *args: real_recovery(*args, retry_sec=0.01)),
        ):
            battery.return_value.configure.side_effect = lambda _cfg: monitoring_ready.set()
            main.main()
            assert [call.kwargs["reason"] for call in sweeps.call_args_list] == [
                "startup", "recovery", "shutdown",
            ]
            assert cloud.sendcommand.call_count == 2
            assert all(not _is_on(call.args[1])
                       for call in cloud.sendcommand.call_args_list)
            battery.assert_called_once()
    finally:
        release.set()
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
