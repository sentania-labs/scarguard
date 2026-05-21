"""Redis request/response handler for test-fire and device status queries.

Mirrors the SnapshotGrabber pattern in the detector service: a daemon thread
subscribes to request channels, performs the action, and publishes the result
to a per-request response channel.
"""

from __future__ import annotations

import json
import logging
import os
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
)

logger = logging.getLogger(__name__)

TEST_FIRE_CHANNEL = "scarguard:deterrent:test-fire"
TEST_FIRE_RESULT_PREFIX = "scarguard:deterrent:test-fire:result:"
STATUS_REQUEST_CHANNEL = "scarguard:deterrent:status-request"
STATUS_RESULT_PREFIX = "scarguard:deterrent:status:result:"
FORCE_OFF_CHANNEL = "scarguard:deterrent:force-off"
FORCE_OFF_RESULT_PREFIX = "scarguard:deterrent:force-off:result:"


class RequestHandler:
    """Handles test-fire and device-status requests from the web service."""

    def __init__(
        self,
        redis_cfg: dict[str, Any],
        act_cfg_ref: AtomicRef[ActuationConfig],
        controller_ref: AtomicRef[TuyaCloudController | None],
    ) -> None:
        self._redis_cfg = redis_cfg
        self._act_cfg_ref = act_cfg_ref
        self._controller_ref = controller_ref
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
        self._shutdown.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

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
                )
                logger.info(
                    "Subscribed to %s, %s, %s",
                    TEST_FIRE_CHANNEL, STATUS_REQUEST_CHANNEL, FORCE_OFF_CHANNEL,
                )
                delay = 5

                for message in pubsub.listen():
                    if self._shutdown.is_set():
                        break
                    if message["type"] != "message":
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
                    elif channel == FORCE_OFF_CHANNEL:
                        self._handle_force_off(client, payload)

            except redis_lib.RedisError:
                if self._shutdown.is_set():
                    break
                logger.exception("Redis error in request handler — retrying in %ds", delay)
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
        request_id = payload.get("request_id", "")
        device_id = payload.get("device_id", "")
        # Second-line clamp — the web route is the authoritative validator
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

        controller = self._controller_ref.get()
        if controller is None:
            client.publish(result_channel, json.dumps({
                "ok": False, "error": "No Tuya credentials configured",
            }))
            return

        act_cfg = self._act_cfg_ref.get()
        device: DeviceConfig | None = None
        for d in act_cfg.devices:
            if d.device_id == device_id:
                device = d
                break

        if device is None:
            client.publish(result_channel, json.dumps({
                "ok": False, "error": f"Device {device_id} not found in config",
            }))
            return

        logger.info(
            "Test-fire: %s (%s) for %.1fs [request_id=%s]",
            device.name, device_id, duration, request_id,
        )
        t0 = time.monotonic()
        result = controller.activate_device(
            device, duration, request_id=request_id, event_type="test_fire",
        )
        wall_sec = time.monotonic() - t0

        if result.stuck:
            self._publish_stuck(
                client, device, request_id=request_id,
                error=result.error or "OFF failed",
            )

        self._persist_test_fire(device, duration, result, wall_sec, request_id)

        client.publish(result_channel, json.dumps({
            "ok": result.success,
            "error": result.error,
            "device_name": device.name,
            "cloud_ack_ms": result.on_ack_ms,
            "stuck": result.stuck,
        }))
        logger.info(
            "Test-fire result: %s — %s",
            device.name, "success" if result.success else (result.error or "failed"),
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
        """Emergency OFF — send OFF to every configured device.

        Ignores ``enabled`` status. A disabled-in-config device that's
        physically stuck on still gets an OFF command. Returns per-device
        ack so the operator can see which devices the cloud actually
        reached.
        """
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
            "Force-OFF executed [request_id=%s] — %d devices, any_failure=%s",
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
