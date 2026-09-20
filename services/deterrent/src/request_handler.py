"""Redis request/response handler for test-fire and device status queries.

Mirrors the SnapshotGrabber pattern in the detector service: a daemon thread
subscribes to request channels, performs the action, and publishes the result
to a per-request response channel.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any

import redis as redis_lib
from actuation_models import (
    ActuationConfig,
    ActuationEvent,
    DeviceAction,
    DeviceConfig,
)
from atomic_ref import AtomicRef
from cloud_controller import ActivationResult, TuyaCloudController
from deterrent_safety import (
    DEFAULT_TEST_FIRE_SEC,
    MAX_TEST_FIRE_SEC,
    clamp_duration,
    group_test_fire_timeout_sec,
    test_fire_timeout_sec,
)

logger = logging.getLogger(__name__)

TEST_FIRE_CHANNEL = "scarguard:deterrent:test-fire"
TEST_FIRE_RESULT_PREFIX = "scarguard:deterrent:test-fire:result:"
STATUS_REQUEST_CHANNEL = "scarguard:deterrent:status-request"
STATUS_RESULT_PREFIX = "scarguard:deterrent:status:result:"
FORCE_OFF_CHANNEL = "scarguard:deterrent:force-off"
FORCE_OFF_RESULT_PREFIX = "scarguard:deterrent:force-off:result:"
TEST_FIRE_GROUP_CHANNEL = "scarguard:deterrent:test-fire-group"
TEST_FIRE_GROUP_RESULT_PREFIX = "scarguard:deterrent:test-fire-group:result:"

# Discriminator for control jobs placed on the deterrent worker's queue, so a
# job is never mistaken for a detection event.
JOB_TEST_FIRE_GROUP = "test_fire_group"
JOB_TEST_FIRE = "test_fire"

# How long a blocking read waits before the loop re-checks the shutdown flag.
# Bounds how long stop() takes on an idle channel.
SHUTDOWN_POLL_SEC = 1.0


class ForceOffLatch:
    """Records that an emergency off happened, so a rotation can see it.

    Force-off sends OFF to every device, but it changes no state that the
    firing path consults: not ``enabled``, not armed, not the shutdown event.
    Before this, a windowed sequence checked those three between cycles,
    found nothing changed, and turned the devices it had just switched off
    straight back on. The panic button worked for a moment and then undid
    itself, which is worse than not working at all.

    A counter rather than a boolean so it never needs clearing: a sequence
    records the value it started under and stops if it has moved. The next
    detection reads the new value and proceeds normally.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0

    def bump(self) -> None:
        with self._lock:
            self._generation += 1

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation


class InFlightGuard:
    """At-most-one claim spanning two threads.

    The request handler claims before enqueuing; the deterrent worker releases
    once the sequence has finished. A plain Lock is the wrong primitive here
    because the claim is made on one thread and released on another, and
    because the window that matters includes the time the job spends queued.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claimed = False

    def claim(self) -> bool:
        """Take the slot if free. Returns False if one is already in flight."""
        with self._lock:
            if self._claimed:
                return False
            self._claimed = True
            return True

    def release(self) -> None:
        with self._lock:
            self._claimed = False

    @property
    def claimed(self) -> bool:
        with self._lock:
            return self._claimed


class RequestHandler:
    """Handles test-fire and device-status requests from the web service."""

    def __init__(
        self,
        redis_cfg: dict[str, Any],
        act_cfg_ref: AtomicRef[ActuationConfig],
        controller_ref: AtomicRef[TuyaCloudController | None],
        job_queue: queue.Queue[dict[str, Any] | None] | None = None,
        in_flight: InFlightGuard | None = None,
        force_off_latch: ForceOffLatch | None = None,
    ) -> None:
        self._redis_cfg = redis_cfg
        self._act_cfg_ref = act_cfg_ref
        self._controller_ref = controller_ref
        self._job_queue = job_queue
        self._in_flight = in_flight or InFlightGuard()
        self._force_off_latch = force_off_latch or ForceOffLatch()
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._shutdown.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="request-handler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop accepting requests and wait for the thread to actually exit.

        Callers rely on this having really stopped: the deterrent service stops
        the handler before joining the worker so a press cannot be accepted
        onto a queue that no longer has a consumer.
        """
        self._shutdown.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                logger.warning(
                    "Request handler did not stop within 10s; it may still "
                    "accept a request that nothing will service",
                )

    def _make_client(self) -> redis_lib.Redis:
        host = self._redis_cfg.get("host", "redis")
        port = int(self._redis_cfg.get("port", 6379))
        password = os.environ.get("REDIS_PASSWORD", "") or None
        return redis_lib.Redis(
            host=host, port=port, password=password, decode_responses=True,
        )

    def _run(self) -> None:
        logger.info("Request handler started")
        delay = 5

        while not self._shutdown.is_set():
            client: redis_lib.Redis | None = None
            pubsub: redis_lib.client.PubSub | None = None
            try:
                client = self._make_client()
                pubsub = client.pubsub()
                pubsub.subscribe(
                    TEST_FIRE_CHANNEL, STATUS_REQUEST_CHANNEL, FORCE_OFF_CHANNEL,
                    TEST_FIRE_GROUP_CHANNEL,
                )
                logger.info(
                    "Subscribed to %s, %s, %s, %s",
                    TEST_FIRE_CHANNEL, STATUS_REQUEST_CHANNEL, FORCE_OFF_CHANNEL,
                    TEST_FIRE_GROUP_CHANNEL,
                )
                delay = 5

                # Polled rather than pubsub.listen(), which blocks forever on
                # an idle channel: the shutdown flag would then only be noticed
                # when a request happened to arrive, so stop() waited out its
                # full join timeout and returned with this thread still
                # subscribed and still accepting work.
                while not self._shutdown.is_set():
                    message = pubsub.get_message(timeout=SHUTDOWN_POLL_SEC)
                    if message is None or message["type"] != "message":
                        continue

                    channel = message["channel"]
                    try:
                        payload = json.loads(message["data"])
                    except json.JSONDecodeError:
                        logger.warning("Malformed request: %s", message["data"])
                        continue

                    if channel == TEST_FIRE_CHANNEL:
                        self._handle_test_fire(client, payload)
                    elif channel == STATUS_REQUEST_CHANNEL:
                        self._handle_status_request(client, payload)
                    elif channel == TEST_FIRE_GROUP_CHANNEL:
                        self._handle_test_fire_group(client, payload)
                    elif channel == FORCE_OFF_CHANNEL:
                        self._handle_force_off(client, payload)

            except redis_lib.RedisError:
                if self._shutdown.is_set():
                    break
                logger.exception("Redis error in request handler - retrying in %ds", delay)
                self._shutdown.wait(delay)
                delay = min(delay * 2, 60)
            finally:
                if pubsub is not None:
                    try:
                        pubsub.unsubscribe()
                        pubsub.close()
                    except Exception:
                        pass
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

        logger.info("Request handler stopped")

    def _handle_test_fire(
        self,
        client: redis_lib.Redis,
        payload: dict[str, Any],
    ) -> None:
        """Hand a single-device test-fire to the worker; do not fire here.

        Same reason the group test-fire moved: this thread is the sole
        consumer of FORCE_OFF_CHANNEL, and firing inline made the emergency
        stop unanswerable for the length of the spray (up to
        MAX_TEST_FIRE_SEC). Fifteen seconds is shorter than a group sequence
        but it is still the panic button not working.

        Running on the worker also means this can no longer overlap a
        detection or a group sequence on the same device, which was the last
        path by which two activations could collide and leave the controller's
        busy set cleared while a device was still energised.

        Validation stays here: it touches no hardware, and an operator who
        typed a bad device id should hear about it immediately rather than
        after waiting behind a queue.
        """
        request_id = payload.get("request_id", "")
        device_id = payload.get("device_id", "")
        # Second-line clamp - the web route is the authoritative validator
        # (returns 400 on out-of-range) but duplicating the cap here means
        # a broken or malicious web peer cannot drive extended actuation.
        duration = clamp_duration(
            payload.get("duration_sec", DEFAULT_TEST_FIRE_SEC),
            max_sec=MAX_TEST_FIRE_SEC,
            default=DEFAULT_TEST_FIRE_SEC,
        )
        result_channel = f"{TEST_FIRE_RESULT_PREFIX}{request_id}"

        if not request_id or not device_id:
            return

        if self._controller_ref.get() is None:
            client.publish(result_channel, json.dumps({
                "ok": False, "error": "No Tuya credentials configured",
            }))
            return

        act_cfg = self._act_cfg_ref.get()
        if not any(d.device_id == device_id for d in act_cfg.devices):
            client.publish(result_channel, json.dumps({
                "ok": False, "error": f"Device {device_id} not found in config",
            }))
            return

        if self._job_queue is None:
            client.publish(result_channel, json.dumps({
                "ok": False, "error": "Deterrent worker unavailable",
            }))
            return

        # Shares the group test-fire's slot: both drive hardware through the
        # same worker, and refusing the second is better than queueing it
        # behind something the operator has stopped watching.
        if not self._in_flight.claim():
            client.publish(result_channel, json.dumps({
                "ok": False, "error": "A test-fire is already in progress",
            }))
            return

        try:
            # Never a blocking put: see _handle_test_fire_group.
            self._job_queue.put_nowait({
                "__job": JOB_TEST_FIRE,
                "device_id": device_id,
                "duration_sec": duration,
                "request_id": request_id,
                "result_channel": result_channel,
                # Must not outlive the caller's wait: see test_fire_timeout_sec.
                "expires_at": time.monotonic() + test_fire_timeout_sec(),
                # Stamped at enqueue, not read at execution. A force-off can
                # land while this job is still queued behind a detection
                # sequence; the worker compares against this and refuses,
                # rather than turning the device back on after the panic
                # button reported success.
                "force_off_gen": self._force_off_latch.generation,
            })
        except queue.Full:
            self._in_flight.release()
            client.publish(result_channel, json.dumps({
                "ok": False,
                "error": "Deterrent worker is saturated, try again shortly",
            }))
            return

        logger.info(
            "Queued test-fire for device %s [rid=%s]", device_id, request_id,
        )

    def _handle_test_fire_group(
        self,
        client: redis_lib.Redis,
        payload: dict[str, Any],
    ) -> None:
        """Hand a group test-fire to the deterrent worker; do not fire here.

        This thread is the only consumer of FORCE_OFF_CHANNEL. A group
        sequence can run for many seconds across several devices, and running
        it inline would make the emergency-off button unanswerable for that
        whole time: the request would sit in the pubsub buffer, the web route
        would time out and report the service as down, and the force-off would
        eventually execute with nobody watching. So the sequence goes to the
        worker thread instead, which is also where the enabled/armed gates and
        both cooldown trackers already live, and which serialises with
        detection firing so two sequences cannot overlap on one device.

        The worker publishes the reply on the result channel when it is done.
        """
        request_id = payload.get("request_id", "")
        group_name = payload.get("group_name", "")
        result_channel = f"{TEST_FIRE_GROUP_RESULT_PREFIX}{request_id}"

        if not request_id or not group_name:
            return

        if self._job_queue is None:
            client.publish(result_channel, json.dumps({
                "ok": False, "error": "Deterrent worker unavailable",
            }))
            return

        # One in flight at a time. Claimed here and released by the worker when
        # the sequence finishes, so the claim covers the queued-but-not-started
        # window too. Probing a lock and releasing it before enqueuing would
        # not: this thread handles requests one at a time, so three presses
        # arriving before the worker dequeues would all see a free lock and all
        # enqueue, which is exactly the stacking the guard exists to stop.
        if not self._in_flight.claim():
            client.publish(result_channel, json.dumps({
                "ok": False,
                "error": "A test-fire is already in progress",
            }))
            return

        try:
            # NEVER a blocking put. The worker queue is bounded, and this thread
            # is the sole consumer of the emergency force-off channel: blocking
            # here would make the panic button unanswerable, which is the whole
            # reason the sequence was moved off this thread in the first place.
            self._job_queue.put_nowait({
                "__job": JOB_TEST_FIRE_GROUP,
                "group_name": group_name,
                "request_id": request_id,
                "result_channel": result_channel,
                # The worker is FIFO and a detection sequence can hold it for
                # minutes, so this job can outlive the caller's wait. Without
                # an expiry the worker would dequeue it afterwards and fire
                # real hardware with nobody watching, after the operator had
                # already been told the request failed.
                "expires_at": time.monotonic() + group_test_fire_timeout_sec(),
                # See the single-device path: the generation belongs to the
                # moment the operator pressed the button, not the moment the
                # worker got around to it.
                "force_off_gen": self._force_off_latch.generation,
            })
        except queue.Full:
            self._in_flight.release()
            client.publish(result_channel, json.dumps({
                "ok": False,
                "error": "Deterrent worker is saturated, try again shortly",
            }))
            return
        logger.info(
            "Queued group test-fire for [%s] [rid=%s]", group_name, request_id,
        )

    @staticmethod
    def _persist_test_fire(
        device: DeviceConfig,
        duration: float,
        result: ActivationResult,
        wall_sec: float,
        request_id: str,
    ) -> None:
        """Persist a test-fire to the actuation DB for a symmetric audit trail."""
        import actuation_db

        action = DeviceAction(
            device_name=device.name,
            device_id=device.device_id,
            device_type=device.type,
            duration_sec=duration,
            delay_before_sec=0.0,
            success=result.success,
            error=result.error,
            cloud_ack_ms=result.on_ack_ms,
            off_attempts=result.off_attempts,
            stuck=result.stuck,
        )
        event = ActuationEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            trigger_class="admin",
            trigger_camera="test-fire",
            trigger_confidence=0.0,
            pre_delay_sec=0.0,
            actions=[action],
            total_duration_sec=wall_sec,
            request_id=request_id,
            event_type="test_fire",
        )
        try:
            actuation_db.insert_event(event)
        except Exception:
            logger.exception("Failed to persist test-fire event [rid=%s]", request_id)

    def _publish_stuck(
        self,
        client: redis_lib.Redis,
        device: DeviceConfig,
        *,
        request_id: str,
        error: str,
    ) -> None:
        """Publish a deterrent:stuck event so the web UI can surface a banner."""
        payload = {
            "device_id": device.device_id,
            "device_name": device.name,
            "request_id": request_id,
            "error": error,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            client.publish("scarguard:deterrent:stuck", json.dumps(payload))
            logger.warning(
                "Published stuck event for %s (%s) [rid=%s]",
                device.name, device.device_id, request_id,
            )
        except Exception:
            logger.exception("Failed to publish stuck event for %s", device.name)

    def _handle_force_off(
        self,
        client: redis_lib.Redis,
        payload: dict[str, Any],
    ) -> None:
        """Emergency OFF - send OFF to every configured device.

        Ignores ``enabled`` status. A disabled-in-config device that's
        physically stuck on still gets an OFF command. Returns per-device
        ack so the operator can see which devices the cloud actually
        reached.
        """
        # Latch FIRST, before any OFF is sent. A windowed sequence checks this
        # between cycles; latching after the OFFs would leave a gap in which
        # the next cycle could re-energise a device this call had just shut.
        self._force_off_latch.bump()

        request_id = payload.get("request_id", "")
        result_channel = f"{FORCE_OFF_RESULT_PREFIX}{request_id}"
        if not request_id:
            return

        controller = self._controller_ref.get()
        if controller is None:
            client.publish(result_channel, json.dumps({
                "ok": False, "error": "No Tuya credentials configured",
            }))
            return

        act_cfg = self._act_cfg_ref.get()
        results: list[dict[str, Any]] = []
        any_failure = False
        for device in act_cfg.devices:
            ok, err = controller.force_off(device, request_id=request_id)
            results.append({
                "device_id": device.device_id,
                "name": device.name,
                "ok": ok,
                "error": err,
            })
            if not ok:
                any_failure = True

        logger.warning(
            "Force-OFF executed [request_id=%s] - %d devices, any_failure=%s",
            request_id, len(results), any_failure,
        )
        client.publish(result_channel, json.dumps({
            "ok": not any_failure,
            "devices": results,
        }))

    def _handle_status_request(
        self,
        client: redis_lib.Redis,
        payload: dict[str, Any],
    ) -> None:
        request_id = payload.get("request_id", "")
        result_channel = f"{STATUS_RESULT_PREFIX}{request_id}"

        if not request_id:
            return

        controller = self._controller_ref.get()
        if controller is None:
            client.publish(result_channel, json.dumps({
                "ok": False, "error": "No Tuya credentials configured",
            }))
            return

        act_cfg = self._act_cfg_ref.get()
        devices: list[dict[str, Any]] = []

        for device in act_cfg.devices:
            status = controller.get_device_status(device.device_id)
            entry: dict[str, Any] = {
                "device_id": device.device_id,
                "name": device.name,
                "type": device.type,
                "enabled": device.enabled,
                "online": status is not None,
            }
            if status is not None:
                entry["battery_pct"] = status.get("battery_percentage")
                entry["switch_state"] = status.get(
                    device.dp_code or "switch_1",
                    status.get("switch_led", status.get("switch")),
                )
            devices.append(entry)

        client.publish(result_channel, json.dumps({
            "ok": True,
            "devices": devices,
        }))
        logger.info("Device status response sent (%d devices)", len(devices))
