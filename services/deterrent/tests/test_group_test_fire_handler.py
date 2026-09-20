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
    InFlightGuard,
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
def _no_external_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cut every path out of the deterrent worker to Redis or the DB.

    Patching sys.modules is not enough: main.py binds ``actuation_db`` at
    import time, and _publish_actuation lazily connects to the configured
    Redis host. Both are wrapped in try/except so nothing crashes, but the
    connect attempt is slow enough on a CI runner to blow a 10s thread join,
    which is how this surfaced: a test that passed locally and failed in CI
    with "worker did not finish".

    Persistence itself is covered by the hash-chain tests.
    """
    import main as deterrent_main

    monkeypatch.setattr(
        deterrent_main.actuation_db, "insert_event", lambda event: None,
    )
    monkeypatch.setattr(
        deterrent_main, "_publish_actuation", lambda holder, cfg, event: None,
    )
    monkeypatch.setattr(
        deterrent_main, "_publish_stuck",
        lambda holder, cfg, device, rid, err: None,
    )


# ── Handler half: enqueue only, never fire ───────────────────────────────────

class TestHandlerEnqueues:
    def _handler(self, q: queue.Queue[Any] | None) -> tuple[RequestHandler, FakeRedis]:
        return (
            RequestHandler({}, AtomicRef(_group_cfg()), AtomicRef(FakeController()), job_queue=q),
            FakeRedis(),
        )

    def test_handler_never_touches_the_controller(self) -> None:
        """The handler thread answers emergency force-off; it must not fire.

        Uses a config with real devices and a real group on purpose. With an
        empty registry this assertion would hold however the handler behaved,
        which is how the first version of this test passed while the handler
        fired hardware directly.
        """
        controller = FakeController()
        q: queue.Queue[Any] = queue.Queue()
        handler = RequestHandler(
            {}, AtomicRef(_group_cfg()), AtomicRef(controller), job_queue=q,
        )
        redis = FakeRedis()
        handler._handle_test_fire_group(redis, {"request_id": "r1", "group_name": "g"})

        assert controller.calls == []
        assert q.qsize() == 1

    def test_second_press_refused_while_job_is_still_queued(self) -> None:
        """The claim must span the queued window, not just the running one."""
        q: queue.Queue[Any] = queue.Queue()
        guard = InFlightGuard()
        handler = RequestHandler(
            {}, AtomicRef(_group_cfg()), AtomicRef(FakeController()),
            job_queue=q, in_flight=guard,
        )
        redis = FakeRedis()
        # Worker has not dequeued anything yet.
        handler._handle_test_fire_group(redis, {"request_id": "r1", "group_name": "g"})
        handler._handle_test_fire_group(redis, {"request_id": "r2", "group_name": "g"})
        handler._handle_test_fire_group(redis, {"request_id": "r3", "group_name": "g"})

        assert q.qsize() == 1, "stacked jobs would keep firing after the operator gave up"
        assert redis.reply_for("r2")["ok"] is False
        assert "already in progress" in redis.reply_for("r2")["error"]
        assert redis.reply_for("r3")["ok"] is False

    def test_full_queue_never_blocks_the_handler(self) -> None:
        """This thread is the sole consumer of emergency force-off.

        Run on a worker thread with a join timeout rather than called directly:
        a blocking put on a full queue would hang forever, and a hanging test
        burns the whole CI job instead of reporting a failure.
        """
        q: queue.Queue[Any] = queue.Queue(maxsize=1)
        q.put_nowait({"filler": True})
        guard = InFlightGuard()
        handler = RequestHandler(
            {}, AtomicRef(_group_cfg()), AtomicRef(FakeController()),
            job_queue=q, in_flight=guard,
        )
        redis = FakeRedis()
        done = threading.Event()

        def call() -> None:
            handler._handle_test_fire_group(redis, {"request_id": "r1", "group_name": "g"})
            done.set()

        t = threading.Thread(target=call, daemon=True)
        t.start()
        assert done.wait(timeout=5.0), (
            "handler blocked on a full queue: emergency force-off is unanswerable"
        )

        reply = redis.reply_for("r1")
        assert reply["ok"] is False
        assert "saturated" in reply["error"]
        # A refused enqueue must not leave the slot claimed forever.
        assert guard.claimed is False

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


class TestWorkerResilience:
    """A raising job must not take the detection path down with it."""

    def test_worker_survives_a_raising_sequence(self) -> None:
        """The worker thread runs all detection actuation.

        If a test-fire kills it, every detection stops firing while the
        container healthcheck keeps reporting healthy, and the only symptom is
        a 502 on the admin page blaming the wrong component.
        """
        import queue as _queue
        import threading as _threading

        import main as deterrent_main

        class Exploding:
            def activate_device(self, *a: Any, **kw: Any) -> Any:
                raise RuntimeError("tuya sdk blew up")

        q: _queue.Queue[Any] = _queue.Queue()
        redis = FakeRedis()
        guard = InFlightGuard()
        guard.claim()
        cfg = _group_cfg(device_count_range=[2, 2])

        q.put({
            "__job": JOB_TEST_FIRE_GROUP,
            "group_name": "g",
            "request_id": "r1",
            "result_channel": f"{TEST_FIRE_GROUP_RESULT_PREFIX}r1",
        })
        q.put(None)  # poison pill so the worker exits after the job

        errors: list[BaseException] = []

        def run() -> None:
            try:
                deterrent_main._worker(
                    q, AtomicRef(cfg), AtomicRef(Exploding()), AtomicRef(True),
                    CooldownTracker(), GroupCooldownTracker(), {}, guard,
                )
            except BaseException as exc:  # noqa: BLE001 - the point of the test
                errors.append(exc)

        # The worker builds its own Redis client lazily; feed it ours.
        import unittest.mock as mock
        with mock.patch.object(deterrent_main, "_publish_raw",
                               lambda h, c, ch, body: redis.publish(ch, json.dumps(body))):
            t = _threading.Thread(target=run, daemon=True)
            t.start()
            t.join(timeout=10)
            assert not t.is_alive(), "worker hung"


        assert errors == [], f"worker died: {errors}"
        assert redis.reply_for("r1")["ok"] is False
        # The slot must be freed, or no further test-fire is ever accepted.
        assert guard.claimed is False

    def test_active_global_cooldown_refuses(self) -> None:
        """Global cooldown gates all actuation, not just per-group."""
        cd = CooldownTracker()
        cd.record()
        cfg = _cfg(
            devices=[_device("v1")],
            groups=[DeterrentGroup(name="g", devices=["v1"], cooldown_seconds=0)],
        )
        cfg.defaults.cooldown_seconds = 300
        controller = FakeController()
        redis, _ = _run_job(cfg, controller, cooldown=cd)

        assert controller.calls == []
        assert "cooldown" in redis.reply_for("r1")["error"].lower()


class TestProductionCallSite:
    """Pins the wiring, not just the helpers it calls.

    execute_plan's deadline, the guard's release point and the
    do-not-burn-cooldown branch were all previously verified only in isolation,
    so mutating the real call site left the suite green.
    """

    def test_worker_passes_the_sequence_cap_to_execute_plan(self, monkeypatch: Any) -> None:
        """Without this the admin path is unbounded: 20 devices x 60s."""
        import group_fire
        import main as deterrent_main
        from deterrent_safety import MAX_GROUP_TEST_FIRE_SEC

        seen: dict[str, Any] = {}
        real = group_fire.execute_plan

        def spy(*a: Any, **kw: Any) -> Any:
            seen["deadline_sec"] = kw.get("deadline_sec")
            return real(*a, **kw)

        monkeypatch.setattr(deterrent_main, "execute_plan", spy)
        _run_job(_group_cfg(device_count_range=[1, 1]), FakeController())

        assert seen["deadline_sec"] == MAX_GROUP_TEST_FIRE_SEC

    def test_claim_is_held_for_the_whole_sequence(self) -> None:
        """Releasing before the sequence makes the guard a no-op while firing."""
        import queue as _queue
        import threading as _threading

        import main as deterrent_main

        observed: list[bool] = []
        gate = _threading.Event()

        class Slow:
            def activate_device(self, device: Any, duration: float, **kw: Any) -> Any:
                observed.append(guard.claimed)
                gate.set()
                return ActivationResult(
                    on_success=True, off_success=True, error=None,
                    on_ack_ms=1.0, off_attempts=1,
                )

        q: _queue.Queue[Any] = _queue.Queue()
        guard = InFlightGuard()
        guard.claim()
        q.put({
            "__job": JOB_TEST_FIRE_GROUP, "group_name": "g", "request_id": "r1",
            "result_channel": f"{TEST_FIRE_GROUP_RESULT_PREFIX}r1",
        })
        q.put(None)
        redis = FakeRedis()
        import unittest.mock as mock
        with mock.patch.object(deterrent_main, "_publish_raw",
                               lambda h, c, ch, body: redis.publish(ch, json.dumps(body))):
            t = _threading.Thread(target=lambda: deterrent_main._worker(
                q, AtomicRef(_group_cfg(device_count_range=[1, 1])),
                AtomicRef(Slow()), AtomicRef(True),
                CooldownTracker(), GroupCooldownTracker(), {}, guard,
            ), daemon=True)
            t.start()
            t.join(timeout=10)
            # Assert inside the patch: a thread that outlives the join escapes
            # the _publish_raw stub and tries to reach a real Redis, which
            # surfaces as an unrelated connection error in a later test.
            assert not t.is_alive(), "worker did not finish"

        assert observed == [True], "guard was released before the hardware ran"
        assert guard.claimed is False

    def test_zero_devices_fired_does_not_burn_a_cooldown(self, monkeypatch: Any) -> None:
        """A no-op test-fire must not lock out real heron detections."""
        import main as deterrent_main
        from group_fire import PlanExecution

        monkeypatch.setattr(
            deterrent_main, "execute_plan",
            lambda *a, **kw: PlanExecution(
                actions=[], pre_delay_sec=0.0, total_duration_sec=0.0,
            ),
        )
        cd = CooldownTracker()
        gc = GroupCooldownTracker()
        redis, _ = _run_job(_group_cfg(), FakeController(), cooldown=cd, group_cooldown=gc)

        assert cd.is_clear(300), "cooldown burned after firing nothing"
        assert gc.is_clear("g", 300)
        reply = redis.reply_for("r1")
        assert reply["ok"] is False
        assert "error" in reply, "a bare failure becomes an unexplained 502"


class TestShutdown:
    """stop() must actually stop, and queued work must be answered."""

    def test_stop_returns_promptly_on_an_idle_channel(self) -> None:
        """pubsub.listen() blocks forever when nothing arrives.

        With it, stop() waited out its full 10s join and returned with the
        thread still subscribed and still accepting work, so the deterrent
        service's "stop the handler before joining the worker" ordering bought
        nothing.
        """
        import time as _time

        import redis as redis_lib

        class IdlePubSub:
            def subscribe(self, *channels: str) -> None:
                pass

            def get_message(self, timeout: float = 0.0) -> None:
                _time.sleep(min(timeout, 0.05))
                return None

            def unsubscribe(self) -> None:
                pass

            def close(self) -> None:
                pass

        class IdleClient:
            def pubsub(self) -> IdlePubSub:
                return IdlePubSub()

            def close(self) -> None:
                pass

        handler = RequestHandler({}, AtomicRef(_group_cfg()), AtomicRef(None))
        handler._make_client = lambda: IdleClient()  # type: ignore[method-assign,assignment]
        handler.start()
        _time.sleep(0.2)

        t0 = _time.monotonic()
        handler.stop()
        elapsed = _time.monotonic() - t0

        assert elapsed < 5.0, f"stop() took {elapsed:.1f}s on an idle channel"
        assert handler._thread is not None and not handler._thread.is_alive()
        assert redis_lib is not None  # keep the import meaningful

    def test_queued_jobs_are_refused_on_shutdown(self) -> None:
        """A job behind the poison pill would otherwise never be answered."""
        import queue as _queue

        import main as deterrent_main

        q: _queue.Queue[Any] = _queue.Queue()
        guard = InFlightGuard()
        guard.claim()
        redis = FakeRedis()
        q.put(None)  # pill first
        q.put({
            "__job": JOB_TEST_FIRE_GROUP, "group_name": "g", "request_id": "r9",
            "result_channel": f"{TEST_FIRE_GROUP_RESULT_PREFIX}r9",
        })

        import unittest.mock as mock
        with mock.patch.object(deterrent_main, "_publish_raw",
                               lambda h, c, ch, body: redis.publish(ch, json.dumps(body))):
            deterrent_main._worker(
                q, AtomicRef(_group_cfg()), AtomicRef(FakeController()), AtomicRef(True),
                CooldownTracker(), GroupCooldownTracker(), {}, guard,
            )

        reply = redis.reply_for("r9")
        assert reply["ok"] is False
        assert "shutting down" in reply["error"]
        assert guard.claimed is False


class TestQueuedJobExpiry:
    """A job that outlives the caller's wait must not fire.

    The worker is FIFO and a detection sequence can hold it for minutes, so a
    queued test-fire can be dequeued long after the web route gave up and told
    the operator it failed. Firing then is worse than not firing: nobody is
    watching the pond when it happens.
    """

    def test_expired_job_fires_nothing(self) -> None:
        import time as _time

        controller = FakeController()
        redis = FakeRedis()
        holder: list[Any] = [redis]
        from main import _run_group_test_fire

        _run_group_test_fire(
            {
                "__job": JOB_TEST_FIRE_GROUP, "group_name": "g",
                "request_id": "r1",
                "result_channel": f"{TEST_FIRE_GROUP_RESULT_PREFIX}r1",
                "expires_at": _time.monotonic() - 1.0,
            },
            AtomicRef(_group_cfg()), AtomicRef(controller), AtomicRef(True),
            CooldownTracker(), GroupCooldownTracker(), holder, {},
        )

        assert controller.calls == [], "fired after the caller gave up"
        reply = redis.reply_for("r1")
        assert reply["ok"] is False
        assert "expired" in reply["error"].lower()

    def test_unexpired_job_still_fires(self) -> None:
        import time as _time

        controller = FakeController()
        redis = FakeRedis()
        holder: list[Any] = [redis]
        from main import _run_group_test_fire

        _run_group_test_fire(
            {
                "__job": JOB_TEST_FIRE_GROUP, "group_name": "g",
                "request_id": "r1",
                "result_channel": f"{TEST_FIRE_GROUP_RESULT_PREFIX}r1",
                "expires_at": _time.monotonic() + 300.0,
            },
            AtomicRef(_group_cfg(device_count_range=[2, 2])),
            AtomicRef(controller), AtomicRef(True),
            CooldownTracker(), GroupCooldownTracker(), holder, {},
        )

        assert sorted(controller.calls) == ["v1", "v2"]

    def test_job_without_expiry_still_fires(self) -> None:
        """Absent expiry must not be read as expired."""
        controller = FakeController()
        redis = FakeRedis()
        holder: list[Any] = [redis]
        from main import _run_group_test_fire

        _run_group_test_fire(
            {
                "__job": JOB_TEST_FIRE_GROUP, "group_name": "g",
                "request_id": "r1",
                "result_channel": f"{TEST_FIRE_GROUP_RESULT_PREFIX}r1",
            },
            AtomicRef(_group_cfg()), AtomicRef(controller), AtomicRef(True),
            CooldownTracker(), GroupCooldownTracker(), holder, {},
        )

        assert controller.calls, "a job with no expiry was treated as expired"

    def test_handler_stamps_an_expiry(self) -> None:
        import queue as _queue
        import time as _time

        q: _queue.Queue[Any] = _queue.Queue()
        handler = RequestHandler(
            {}, AtomicRef(_group_cfg()), AtomicRef(FakeController()), job_queue=q,
        )
        handler._handle_test_fire_group(
            FakeRedis(), {"request_id": "r1", "group_name": "g"},
        )
        job = q.get_nowait()
        assert job["expires_at"] > _time.monotonic()


def _capture(seen: dict[str, Any]) -> Any:
    """Record execute_plan's arguments without running it.

    Deliberately does NOT call through. These tests assert what the production
    call site passes, and running the real sequence would burn the window in
    wall-clock: a 60s cap made one test take 60 real seconds. Rotation
    behaviour itself is tested against a fake clock in test_group_fire.
    """
    from group_fire import PlanExecution

    def spy(*a: Any, **kw: Any) -> PlanExecution:
        seen.update(kw)
        return PlanExecution(actions=[], pre_delay_sec=0.0, total_duration_sec=0.0)

    return spy


class TestTestFireRotatesLikeADetection:
    """The button claims it runs the group's real plan, so it must rotate.

    Otherwise the one behaviour an operator most wants to see before heron
    season, the group working a position, is the one thing the test cannot
    show.
    """

    def test_a_configured_window_makes_the_test_fire_rotate(self, monkeypatch: Any) -> None:
        import main as deterrent_main

        seen: dict[str, Any] = {}
        monkeypatch.setattr(deterrent_main, "execute_plan", _capture(seen))
        cfg = _group_cfg(device_count_range=[1, 1], group_duration_range=[20.0, 20.0])
        _run_job(cfg, FakeController())

        assert seen["rotate"] is True
        assert seen["deadline_sec"] == 20.0

    def test_no_window_means_one_pass(self, monkeypatch: Any) -> None:
        import main as deterrent_main
        from deterrent_safety import MAX_GROUP_TEST_FIRE_SEC

        seen: dict[str, Any] = {}
        monkeypatch.setattr(deterrent_main, "execute_plan", _capture(seen))
        _run_job(_group_cfg(device_count_range=[1, 1]), FakeController())

        assert seen["rotate"] is False
        assert seen["deadline_sec"] == MAX_GROUP_TEST_FIRE_SEC

    def test_window_is_capped_by_the_test_fire_limit(self, monkeypatch: Any) -> None:
        """A button press must not start a 300s sequence."""
        import main as deterrent_main
        from deterrent_safety import MAX_GROUP_TEST_FIRE_SEC

        seen: dict[str, Any] = {}
        monkeypatch.setattr(deterrent_main, "execute_plan", _capture(seen))
        cfg = _group_cfg(device_count_range=[1, 1], group_duration_range=[300.0, 300.0])
        _run_job(cfg, FakeController())

        assert seen["deadline_sec"] == MAX_GROUP_TEST_FIRE_SEC


class TestZeroDeviceSequenceDoesNotBurnCooldown:
    """A detection that fires nothing must not suppress the next one.

    _fire_group used to return True unconditionally, so a sequence stopped
    before its first activation (emergency off latched between the detection
    arriving and the first gate) still recorded both cooldowns and wrote an
    empty audit row. The operator would clear the latch and the next heron
    would get nothing.
    """

    def test_fire_group_reports_not_fired_when_nothing_ran(self, monkeypatch: Any) -> None:
        import group_fire
        import main as deterrent_main
        from group_fire import PlanExecution

        monkeypatch.setattr(
            deterrent_main, "execute_plan",
            lambda *a, **kw: PlanExecution(
                actions=[], pre_delay_sec=0.0, total_duration_sec=0.0,
            ),
        )
        cfg = _group_cfg()
        group = cfg.groups[0]
        fired = deterrent_main._fire_group(
            group, cfg, FakeController(), {"camera_name": "c", "class_name": "heron"},
            None, 0, [FakeRedis()], {},
        )
        assert fired is False, "a zero-device sequence reported as fired"
        assert group_fire is not None

    def test_a_real_sequence_still_reports_fired(self, monkeypatch: Any) -> None:
        import main as deterrent_main

        cfg = _group_cfg(device_count_range=[1, 1])
        group = cfg.groups[0]
        fired = deterrent_main._fire_group(
            group, cfg, FakeController(), {"camera_name": "c", "class_name": "heron"},
            None, 0, [FakeRedis()], {},
        )
        assert fired is True


class TestTestFireIsInterruptible:
    """The admin button drives the same hardware as a detection.

    Emergency off must reach it too, or the panic button works for a heron and
    not for the button sitting next to it.
    """

    def test_test_fire_passes_an_abort_hook(self, monkeypatch: Any) -> None:
        import main as deterrent_main

        seen: dict[str, Any] = {}
        monkeypatch.setattr(deterrent_main, "execute_plan", _capture(seen))
        _run_job(_group_cfg(device_count_range=[1, 1]), FakeController())

        assert seen.get("should_continue") is not None, (
            "the admin test-fire cannot be stopped by emergency off"
        )
        assert callable(seen["should_continue"])


class TestSingleDeviceTestFireOnTheWorker:
    """Closes #206 item 1.

    _handle_test_fire used to call activate_device on the request-handler
    thread, which is the sole consumer of FORCE_OFF_CHANNEL. For up to
    MAX_TEST_FIRE_SEC the emergency stop was unanswerable. Shorter than a
    group sequence, but still the panic button not working.

    Running on the worker also means it can no longer overlap a detection or
    a group sequence on the same device, which was the last path by which two
    activations could collide and clear the controller's busy flag while a
    device was still energised.
    """

    def _handler(self, q: Any, controller: Any) -> tuple[RequestHandler, FakeRedis]:
        return (
            RequestHandler({}, AtomicRef(_group_cfg()), AtomicRef(controller), job_queue=q),
            FakeRedis(),
        )

    @staticmethod
    def _reply(redis: FakeRedis, rid: str) -> dict[str, Any]:
        from request_handler import TEST_FIRE_RESULT_PREFIX
        for ch, body in redis.published:
            if ch == f"{TEST_FIRE_RESULT_PREFIX}{rid}":
                return body
        raise AssertionError("no reply")

    def test_handler_does_not_fire(self) -> None:
        import queue as _queue

        from request_handler import JOB_TEST_FIRE

        controller = FakeController()
        q: _queue.Queue[Any] = _queue.Queue()
        handler, redis = self._handler(q, controller)
        handler._handle_test_fire(redis, {"request_id": "r1", "device_id": "id-v1"})

        assert controller.calls == [], "fired on the thread that answers force-off"
        job = q.get_nowait()
        assert job["__job"] == JOB_TEST_FIRE
        assert job["device_id"] == "id-v1"

    def test_unknown_device_is_refused_without_queueing(self) -> None:
        """No hardware involved, so answer immediately rather than queueing."""
        import queue as _queue

        controller = FakeController()
        q: _queue.Queue[Any] = _queue.Queue()
        handler, redis = self._handler(q, controller)
        handler._handle_test_fire(redis, {"request_id": "r1", "device_id": "nope"})

        assert q.qsize() == 0
        assert "not found" in self._reply(redis, "r1")["error"]

    def test_worker_fires_the_requested_device(self) -> None:
        from main import _run_test_fire
        from request_handler import JOB_TEST_FIRE, TEST_FIRE_RESULT_PREFIX

        controller = FakeController()
        redis = FakeRedis()
        _run_test_fire(
            {
                "__job": JOB_TEST_FIRE, "device_id": "id-v1", "duration_sec": 3.0,
                "request_id": "r1",
                "result_channel": f"{TEST_FIRE_RESULT_PREFIX}r1",
            },
            AtomicRef(_group_cfg()), AtomicRef(controller), [redis], {},
        )
        assert controller.calls == ["v1"]
        assert self._reply(redis, "r1")["ok"] is True

    def test_worker_refuses_an_expired_job(self) -> None:
        import time as _time

        from main import _run_test_fire
        from request_handler import JOB_TEST_FIRE, TEST_FIRE_RESULT_PREFIX

        controller = FakeController()
        redis = FakeRedis()
        _run_test_fire(
            {
                "__job": JOB_TEST_FIRE, "device_id": "id-v1", "duration_sec": 3.0,
                "request_id": "r1",
                "result_channel": f"{TEST_FIRE_RESULT_PREFIX}r1",
                "expires_at": _time.monotonic() - 1.0,
            },
            AtomicRef(_group_cfg()), AtomicRef(controller), [redis], {},
        )
        assert controller.calls == []
        assert "expired" in self._reply(redis, "r1")["error"].lower()

    def test_disarmed_does_not_block_a_single_device_test(self) -> None:
        """Deliberate: this is the check-the-valve diagnostic.

        An operator standing at the pond with the system disarmed must still
        be able to test a valve. The group test-fire does gate on armed,
        because it drives the whole group the way a detection would.
        """
        from main import _run_test_fire
        from request_handler import JOB_TEST_FIRE, TEST_FIRE_RESULT_PREFIX

        cfg = _group_cfg()
        cfg.enabled = False
        controller = FakeController()
        redis = FakeRedis()
        _run_test_fire(
            {
                "__job": JOB_TEST_FIRE, "device_id": "id-v1", "duration_sec": 3.0,
                "request_id": "r1",
                "result_channel": f"{TEST_FIRE_RESULT_PREFIX}r1",
            },
            AtomicRef(cfg), AtomicRef(controller), [redis], {},
        )
        assert controller.calls == ["v1"]


class TestZeroDeviceReasonIsAccurate:
    """Which of the two happened matters to whoever reads it.

    Reporting "the window elapsed" to an operator who just pressed emergency
    off sends them looking at group_duration_range instead of at the button
    they pressed.
    """

    def _run_with(self, monkeypatch: Any, *, aborted: bool) -> dict[str, Any]:
        import main as deterrent_main
        from group_fire import PlanExecution

        monkeypatch.setattr(
            deterrent_main, "execute_plan",
            lambda *a, **kw: PlanExecution(
                actions=[], pre_delay_sec=0.0, total_duration_sec=0.0,
                aborted=aborted,
            ),
        )
        redis, _ = _run_job(_group_cfg(), FakeController())
        return redis.reply_for("r1")

    def test_an_abort_says_so(self, monkeypatch: Any) -> None:
        reply = self._run_with(monkeypatch, aborted=True)
        assert reply["aborted"] is True
        assert "emergency off" in reply["error"]
        assert "window" not in reply["error"]

    def test_an_elapsed_window_says_so(self, monkeypatch: Any) -> None:
        reply = self._run_with(monkeypatch, aborted=False)
        assert reply["aborted"] is False
        assert "window" in reply["error"]
