"""ScarGuard deterrent — subscribes to Redis detections and triggers Tuya devices."""

import json
import logging
import os
import pathlib
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

import redis as redis_lib
import yaml
from actuation_models import ActuationConfig, ActuationEvent, DeviceAction
from atomic_ref import AtomicRef
from battery_monitor import BatteryMonitor
from cloud_controller import TuyaCloudController
from config_watcher import ConfigWatcher
from cooldown import CooldownTracker
from randomizer import build_random_plan

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/scarguard.yml")
CHANNEL = "scarguard:detections"
ACTUATION_CHANNEL = "scarguard:actuations"

_REDIS_RECONNECT_DELAY = 5
_REDIS_MAX_RECONNECT_DELAY = 60


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def setup_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,
    )


def parse_actuation_config(cfg: dict[str, Any]) -> ActuationConfig:
    """Parse the ``deterrent`` section of the config, returning defaults if absent."""
    raw = cfg.get("deterrent", {})
    if not raw:
        return ActuationConfig()
    return ActuationConfig(**raw)


def build_controller(act_cfg: ActuationConfig) -> TuyaCloudController | None:
    """Build a Cloud controller from config, or None if credentials are missing."""
    if act_cfg.tuya is None:
        return None
    return TuyaCloudController(
        api_key=act_cfg.tuya.api_key,
        api_secret=act_cfg.tuya.api_secret,
        api_region=act_cfg.tuya.api_region,
    )


# ---------------------------------------------------------------------------
# Worker thread — processes events sequentially (tinytuya.Cloud isn't
# documented as thread-safe, and actuation sequences are inherently serial).
# ---------------------------------------------------------------------------

def _worker(
    event_queue: queue.Queue[dict[str, Any] | None],
    act_cfg_ref: AtomicRef[ActuationConfig],
    controller_ref: AtomicRef[TuyaCloudController | None],
    armed_ref: AtomicRef[bool],
    cooldown: CooldownTracker,
    redis_cfg: dict[str, Any],
) -> None:
    """Consume detection events and run actuation sequences."""
    logger.info("Deterrent worker thread started")

    # Mutable container so the lazily-created Redis client persists across calls.
    pub_holder: list[redis_lib.Redis | None] = [None]

    while True:
        event = event_queue.get()
        if event is None:  # poison pill — shutdown
            break

        act_cfg = act_cfg_ref.get()
        controller = controller_ref.get()

        # Skip system/internal events (battery alerts, camera health, etc.)
        class_name = event.get("class_name", "")
        if class_name in ("low_battery", "camera_offline"):
            continue

        # Gate checks
        if not act_cfg.enabled:
            logger.debug("Actuation disabled — ignoring event")
            continue
        if not armed_ref.get():
            logger.debug("System disarmed — ignoring event")
            continue
        if controller is None:
            logger.warning("No Tuya credentials configured — cannot actuate")
            continue

        cooldown_sec = act_cfg.defaults.cooldown_seconds
        if not cooldown.is_clear(cooldown_sec):
            remaining = cooldown.seconds_remaining(cooldown_sec)
            logger.info("Cooldown active (%.0fs remaining) — skipping", remaining)
            continue

        enabled_devices = [d for d in act_cfg.devices if d.enabled]
        if not enabled_devices:
            logger.warning("No enabled devices — cannot actuate")
            continue

        camera_name = event.get("camera_name", "unknown")
        confidence = event.get("confidence", 0.0)
        logger.info(
            "Actuation triggered: %s from %s (conf=%.2f) — %d device(s) available",
            class_name, camera_name, confidence, len(enabled_devices),
        )

        # Build randomised plan
        selected, durations, inter_delays, pre_delay = build_random_plan(
            enabled_devices, act_cfg.defaults,
        )

        # Execute
        t_start = time.monotonic()
        actions: list[DeviceAction] = []

        if pre_delay > 0:
            logger.debug("Pre-delay: %.1fs", pre_delay)
            time.sleep(pre_delay)

        for i, device in enumerate(selected):
            if inter_delays[i] > 0:
                logger.debug("Inter-device delay: %.1fs", inter_delays[i])
                time.sleep(inter_delays[i])

            duration = durations[i]
            logger.info(
                "Firing device %s (%s) for %.1fs",
                device.name, device.type, duration,
            )
            success, error = controller.activate_device(device, duration)
            actions.append(DeviceAction(
                device_name=device.name,
                device_id=device.device_id,
                device_type=device.type,
                duration_sec=duration,
                delay_before_sec=inter_delays[i],
                success=success,
                error=error,
            ))

        total_duration = time.monotonic() - t_start
        cooldown.record()

        actuation_event = ActuationEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            trigger_class=class_name,
            trigger_camera=camera_name,
            trigger_confidence=confidence,
            pre_delay_sec=pre_delay,
            actions=actions,
            total_duration_sec=round(total_duration, 2),
        )

        successes = sum(1 for a in actions if a.success)
        logger.info(
            "Actuation complete: %d/%d devices fired in %.1fs",
            successes, len(actions), total_duration,
        )

        # Publish actuation event to Redis
        _publish_actuation(pub_holder, redis_cfg, actuation_event)

    logger.info("Deterrent worker thread stopped")


def _publish_actuation(
    holder: list[redis_lib.Redis | None],
    redis_cfg: dict[str, Any],
    event: ActuationEvent,
) -> None:
    """Publish an actuation event to Redis.  Lazily connects."""
    try:
        client = holder[0]
        if client is None:
            host = redis_cfg.get("host", "redis")
            port = int(redis_cfg.get("port", 6379))
            password = os.environ.get("REDIS_PASSWORD", "") or None
            client = redis_lib.Redis(host=host, port=port, password=password, decode_responses=True)
            holder[0] = client
        client.publish(ACTUATION_CHANNEL, event.model_dump_json())
    except Exception:
        logger.exception("Failed to publish actuation event")
        holder[0] = None  # force reconnect on next attempt


# ---------------------------------------------------------------------------
# Subscribe loop — mirrors the notifier pattern
# ---------------------------------------------------------------------------

def subscribe_loop(
    redis_cfg: dict[str, Any],
    event_queue: queue.Queue[dict[str, Any] | None],
    shutdown_event: threading.Event,
) -> None:
    """Connect to Redis and forward detection events to the worker queue."""
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))
    delay = _REDIS_RECONNECT_DELAY

    while not shutdown_event.is_set():
        client: redis_lib.Redis | None = None
        pubsub: redis_lib.client.PubSub | None = None
        try:
            redis_password = os.environ.get("REDIS_PASSWORD", "") or None
            client = redis_lib.Redis(
                host=host, port=port, password=redis_password, decode_responses=True,
            )
            pubsub = client.pubsub()
            pubsub.subscribe(CHANNEL)
            logger.info("Subscribed to Redis channel: %s", CHANNEL)
            delay = _REDIS_RECONNECT_DELAY

            pathlib.Path("/tmp/healthy").touch(exist_ok=True)

            for message in pubsub.listen():
                if shutdown_event.is_set():
                    break
                if message["type"] != "message":
                    continue

                pathlib.Path("/tmp/healthy").touch(exist_ok=True)

                try:
                    event = json.loads(message["data"])
                except json.JSONDecodeError:
                    logger.warning("Malformed message: %s", message["data"])
                    continue

                logger.debug(
                    "Detection: %s from %s (conf=%.2f)",
                    event.get("class_name"),
                    event.get("camera_name"),
                    event.get("confidence", 0.0),
                )
                try:
                    event_queue.put_nowait(event)
                except queue.Full:
                    logger.warning("Event queue full — dropping event")

        except redis_lib.RedisError:
            if shutdown_event.is_set():
                break
            logger.exception("Redis connection lost — retrying in %ds", delay)
            time.sleep(delay)
            delay = min(delay * 2, _REDIS_MAX_RECONNECT_DELAY)
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

    logger.info("Subscription loop exited")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config()
    setup_logging(cfg.get("system", {}).get("log_level", "info"))
    logger.info("ScarGuard deterrent service starting")

    act_cfg = parse_actuation_config(cfg)
    controller = build_controller(act_cfg)

    act_cfg_ref: AtomicRef[ActuationConfig] = AtomicRef(act_cfg)
    controller_ref: AtomicRef[TuyaCloudController | None] = AtomicRef(controller)
    armed_ref: AtomicRef[bool] = AtomicRef(cfg.get("system", {}).get("armed", True))

    cooldown = CooldownTracker()
    event_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=64)

    if not act_cfg.enabled:
        logger.info("Actuation disabled in config — service will idle until enabled")
    elif controller is None:
        logger.warning("Actuation enabled but Tuya credentials missing — check config")
    else:
        enabled_count = sum(1 for d in act_cfg.devices if d.enabled)
        logger.info(
            "Actuation enabled — %d device(s) registered, cooldown %ds",
            enabled_count, act_cfg.defaults.cooldown_seconds,
        )

    # Battery monitor
    redis_cfg = cfg.get("redis", {})
    battery_monitor: BatteryMonitor | None = None
    if controller is not None:
        redis_password = os.environ.get("REDIS_PASSWORD", "") or None
        batt_redis = redis_lib.Redis(
            host=redis_cfg.get("host", "redis"),
            port=int(redis_cfg.get("port", 6379)),
            password=redis_password,
            decode_responses=True,
        )
        battery_monitor = BatteryMonitor(controller, batt_redis)
        battery_monitor.configure(act_cfg)
        if act_cfg.battery_monitor.enabled:
            battery_monitor.start()

    # Shutdown signal handling
    shutdown_event = threading.Event()

    def _shutdown(sig: int, _frame: object) -> None:
        logger.info("Received signal %s — shutting down", sig)
        shutdown_event.set()
        event_queue.put(None)  # poison pill for worker

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Config hot-reload
    def _on_config_change(new_cfg: dict[str, Any]) -> None:
        nonlocal battery_monitor
        new_act = parse_actuation_config(new_cfg)
        new_armed = new_cfg.get("system", {}).get("armed", True)

        # Rebuild controller if credentials changed
        old_act = act_cfg_ref.get()
        if new_act.tuya != old_act.tuya:
            new_controller = build_controller(new_act)
            controller_ref.set(new_controller)
            logger.info("Tuya Cloud controller rebuilt (credentials changed)")

            # Create battery monitor if credentials appeared for the first time
            if new_controller is not None and battery_monitor is None:
                redis_password = os.environ.get("REDIS_PASSWORD", "") or None
                batt_redis = redis_lib.Redis(
                    host=redis_cfg.get("host", "redis"),
                    port=int(redis_cfg.get("port", 6379)),
                    password=redis_password,
                    decode_responses=True,
                )
                battery_monitor = BatteryMonitor(new_controller, batt_redis)
                logger.info("Battery monitor created (credentials now available)")

        act_cfg_ref.set(new_act)
        armed_ref.set(new_armed)

        # Start/stop battery monitor on config change
        if battery_monitor is not None:
            battery_monitor.configure(new_act)
            if new_act.battery_monitor.enabled:
                battery_monitor.start()  # no-op if already running
            else:
                battery_monitor.stop()

        enabled_count = sum(1 for d in new_act.devices if d.enabled)
        logger.info(
            "Config reloaded — actuation %s, %d device(s), armed=%s",
            "enabled" if new_act.enabled else "disabled",
            enabled_count,
            new_armed,
        )

    watcher = ConfigWatcher(CONFIG_PATH, _on_config_change)
    watcher.start()

    # Start worker thread
    worker_thread = threading.Thread(
        target=_worker,
        name="deterrent-worker",
        daemon=True,
        args=(event_queue, act_cfg_ref, controller_ref, armed_ref, cooldown, redis_cfg),
    )
    worker_thread.start()

    # Subscribe loop blocks until shutdown
    subscribe_loop(redis_cfg, event_queue, shutdown_event)

    # Cleanup
    event_queue.put(None)  # ensure worker exits
    worker_thread.join(timeout=10)
    watcher.stop()
    if battery_monitor is not None:
        battery_monitor.stop()

    logger.info("Deterrent service stopped cleanly")


if __name__ == "__main__":
    main()
