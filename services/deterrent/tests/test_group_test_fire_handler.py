"""Tests for the admin group test-fire, both halves.

The request handler only enqueues: it must never fire hardware, because it is
the sole consumer of the emergency force-off channel. The worker does the
firing and owns every gate. Both halves are tested here so the split itself is
pinned: if someone moves the firing back onto the handler thread,
``test_handler_never_touches_the_controller`` fails.
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any

import pytest
from actuation_models import (
    ActuationConfig,
    ActuationDefaults,
    DeterrentGroup,
    DeviceConfig,
    TuyaCredentials,
)
from atomic_ref import AtomicRef
from cloud_controller import ActivationResult
from cooldown import CooldownTracker, GroupCooldownTracker
from request_handler import (
    JOB_TEST_FIRE_GROUP,
    TEST_FIRE_GROUP_RESULT_PREFIX,
    RequestHandler,
)


def _device(name: str, *, enabled: bool = True) -> DeviceConfig:
    return DeviceConfig(name=name, device_id=f"id-{name}", type="sprinkler", enabled=enabled)


class FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, json.loads(payload)))

    def reply_for(self, request_id: str) -> dict[str, Any]:
        channel = f"{TEST_FIRE_GROUP_RESULT_PREFIX}{request_id}"
        for ch, body in self.published:
            if ch == channel:
                return body
        raise AssertionError(f"no reply published on {channel}")


class FakeController:
    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self._fail_on = fail_on or set()

    def activate_device(
        self, device: DeviceConfig, duration: float, *, request_id: str, event_type: str,
    ) -> ActivationResult:
        self.calls.append(device.name)
        if device.name in self._fail_on:
            return ActivationResult(
                on_success=False, off_success=None, error="boom",
                on_ack_ms=None, off_attempts=0,
            )
        return ActivationResult(
            on_success=True, off_success=True, error=None, on_ack_ms=5.0, off_attempts=1,
        )


def _cfg(**kw: Any) -> ActuationConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "tuya": TuyaCredentials(api_key="k", api_secret="s"),
        "devices": [],
        "groups": [],
        "defaults": ActuationDefaults(
            device_count_range=[1, 1],
            spray_duration_range=[0.0, 0.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
            cooldown_seconds=0,
        ),
    }
    base.update(kw)
    return ActuationConfig(**base)


@pytest.fixture(autouse=True)
def _no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Persistence is covered by the hash-chain tests."""
    import sys
    import types

    stub = types.ModuleType("actuation_db")
    stub.insert_event = lambda event: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "actuation_db", stub)


# ── Handler half: enqueue only, never fire ───────────────────────────────────

class TestHandlerEnqueues:
    def _handler(self, q: queue.Queue[Any] | None) -> tuple[RequestHandler, FakeRedis]:
        return RequestHandler({}, AtomicRef(_cfg()), AtomicRef(FakeController()), job_queue=q), FakeRedis()

    def test_handler_never_touches_the_controller(self) -> None:
        """The handler thread answers emergency force-off; it must not block."""
        controller = FakeController()
        q: queue.Queue[Any] = queue.Queue()
        handler = RequestHandler({}, AtomicRef(_cfg()), AtomicRef(controller), job_queue=q)
        redis = FakeRedis()
        handler._handle_test_fire_group(redis, {"request_id": "r1", "group_name": "g"})

        assert controller.calls == []
        assert q.qsize() == 1

    def test_enqueued_job_carries_what_the_worker_needs(self) -> None:
        q: queue.Queue[Any] = queue.Queue()
        handler, redis = self._handler(q)
        handler._handle_test_fire_group(redis, {"request_id": "r1", "group_name": "g"})

        job = q.get_nowait()
        assert job["__job"] == JOB_TEST_FIRE_GROUP
        assert job["group_name"] == "g"
        assert job["request_id"] == "r1"
        assert job["result_channel"] == f"{TEST_FIRE_GROUP_RESULT_PREFIX}r1"

    def test_missing_group_name_publishes_nothing(self) -> None:
        q: queue.Queue[Any] = queue.Queue()
        handler, redis = self._handler(q)
        handler._handle_test_fire_group(redis, {"request_id": "r1"})
        assert redis.published == []
        assert q.qsize() == 0

    def test_missing_request_id_publishes_nothing(self) -> None:
        q: queue.Queue[Any] = queue.Queue()
        handler, redis = self._handler(q)
        handler._handle_test_fire_group(redis, {"group_name": "g"})
        assert redis.published == []
        assert q.qsize() == 0

    def test_no_worker_queue_refuses(self) -> None:
        handler, redis = self._handler(None)
        handler._handle_test_fire_group(redis, {"request_id": "r1", "group_name": "g"})
        assert redis.reply_for("r1")["ok"] is False

    def test_second_request_is_refused_while_one_is_in_flight(self) -> None:
        """Queueing them would keep firing long after the operator gave up."""
        q: queue.Queue[Any] = queue.Queue()
        lock = threading.Lock()
        handler = RequestHandler(
            {}, AtomicRef(_cfg()), AtomicRef(FakeController()),
            job_queue=q, test_fire_lock=lock,
        )
        redis = FakeRedis()
        lock.acquire()  # simulate the worker mid-sequence
        try:
            handler._handle_test_fire_group(redis, {"request_id": "r2", "group_name": "g"})
        finally:
            lock.release()

        reply = redis.reply_for("r2")
        assert reply["ok"] is False
        assert "already in progress" in reply["error"]
        assert q.qsize() == 0


# ── Worker half: the gates that keep hardware still ──────────────────────────

def _run_job(
    cfg: ActuationConfig,
    controller: FakeController | None,
    *,
    armed: bool = True,
    cooldown: CooldownTracker | None = None,
    group_cooldown: GroupCooldownTracker | None = None,
) -> tuple[FakeRedis, FakeController | None]:
    from main import _run_group_test_fire

    redis = FakeRedis()
    holder: list[Any] = [redis]
    _run_group_test_fire(
        {
            "__job": JOB_TEST_FIRE_GROUP,
            "group_name": "g",
            "request_id": "r1",
            "result_channel": f"{TEST_FIRE_GROUP_RESULT_PREFIX}r1",
        },
        AtomicRef(cfg),
        AtomicRef(controller),
        AtomicRef(armed),
        cooldown or CooldownTracker(),
        group_cooldown or GroupCooldownTracker(),
        threading.Lock(),
        holder,
        {},
    )
    return redis, controller


def _group_cfg(**group_kw: Any) -> ActuationConfig:
    return _cfg(
        devices=[_device("v1"), _device("v2")],
        groups=[DeterrentGroup(name="g", devices=["v1", "v2"], cooldown_seconds=0, **group_kw)],
    )


class TestWorkerGates:
    def test_disabled_deterrent_fires_nothing(self) -> None:
        """deterrent.enabled: false is the kill switch; a button must respect it."""
        cfg = _group_cfg()
        cfg.enabled = False
        controller = FakeController()
        redis, _ = _run_job(cfg, controller)

        assert controller.calls == []
        assert redis.reply_for("r1")["ok"] is False
        assert "disabled" in redis.reply_for("r1")["error"].lower()

    def test_disarmed_system_fires_nothing(self) -> None:
        controller = FakeController()
        redis, _ = _run_job(_group_cfg(), controller, armed=False)

        assert controller.calls == []
        assert "disarmed" in redis.reply_for("r1")["error"].lower()

    def test_no_controller_refuses(self) -> None:
        redis, _ = _run_job(_group_cfg(), None)
        assert redis.reply_for("r1")["ok"] is False

    def test_unknown_group_fires_nothing(self) -> None:
        controller = FakeController()
        redis, _ = _run_job(_cfg(), controller)
        assert controller.calls == []
        assert "not found" in redis.reply_for("r1")["error"]

    def test_group_with_only_disabled_devices_fires_nothing(self) -> None:
        cfg = _cfg(
            devices=[_device("v1", enabled=False)],
            groups=[DeterrentGroup(name="g", devices=["v1"], cooldown_seconds=0)],
        )
        controller = FakeController()
        redis, _ = _run_job(cfg, controller)
        assert controller.calls == []
        assert "no enabled devices" in redis.reply_for("r1")["error"]

    def test_active_group_cooldown_refuses(self) -> None:
        gc = GroupCooldownTracker()
        gc.record("g")
        cfg = _cfg(
            devices=[_device("v1")],
            groups=[DeterrentGroup(name="g", devices=["v1"], cooldown_seconds=300)],
        )
        controller = FakeController()
        redis, _ = _run_job(cfg, controller, group_cooldown=gc)

        assert controller.calls == []
        assert "cooldown" in redis.reply_for("r1")["error"].lower()


class TestWorkerFires:
    def test_happy_path_reports_per_device_detail(self) -> None:
        controller = FakeController()
        redis, _ = _run_job(_group_cfg(device_count_range=[2, 2]), controller)

        reply = redis.reply_for("r1")
        assert reply["ok"] is True
        assert reply["devices_fired"] == 2
        assert reply["devices_succeeded"] == 2
        assert sorted(controller.calls) == ["v1", "v2"]

    def test_partial_success_is_not_reported_as_total_failure(self) -> None:
        """Three of four firing is information, not an error."""
        controller = FakeController(fail_on={"v2"})
        redis, _ = _run_job(_group_cfg(device_count_range=[2, 2]), controller)

        reply = redis.reply_for("r1")
        assert reply["ok"] is True
        assert reply["devices_fired"] == 2
        assert reply["devices_succeeded"] == 1

    def test_all_devices_failing_is_a_failure(self) -> None:
        controller = FakeController(fail_on={"v1", "v2"})
        redis, _ = _run_job(_group_cfg(device_count_range=[2, 2]), controller)
        assert redis.reply_for("r1")["ok"] is False

    def test_firing_consumes_both_cooldowns(self) -> None:
        """A test-fire is the same physical event as a detection."""
        cd = CooldownTracker()
        gc = GroupCooldownTracker()
        assert cd.is_clear(300)
        assert gc.is_clear("g", 300)

        _run_job(_group_cfg(device_count_range=[2, 2]), FakeController(),
                 cooldown=cd, group_cooldown=gc)

        assert not cd.is_clear(300)
        assert not gc.is_clear("g", 300)
