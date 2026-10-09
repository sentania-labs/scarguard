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
    # already-armed deadline must issue another OFF after that late ON.
    never_release.set()
    _wait_for_workers_to_exit("tuya-on")
    deadline = time.monotonic() + 1
    while len(call_times) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [value for value, _when in call_times] == [False, True]
    timers[0].fire()
    assert [value for value, _when in call_times] == [False, True, False]


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
