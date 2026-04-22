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
import uuid
from datetime import datetime, timezone
from typing import Any

import actuation_db
import redis as redis_lib
import yaml
from actuation_models import (
    ActuationConfig,
    ActuationEvent,
    DeterrentGroup,
    DeviceAction,
    DeviceConfig,
)
from atomic_ref import AtomicRef
from battery_monitor import BatteryMonitor
from cloud_controller import TuyaCloudController
from config_watcher import ConfigWatcher
from cooldown import CooldownTracker, GroupCooldownTracker
from deterrent_safety import (
    MAX_ACTUATION_SEC,
    clamp_duration,
)
from event_signing import load_key_from_env, verify_event
from healthcheck import start_heartbeat
from randomizer import build_random_plan
from request_handler import RequestHandler

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/scarguard.yml")
CHANNEL = "scarguard:detections"
ACTUATION_CHANNEL = "scarguard:actuations"
STUCK_CHANNEL = "scarguard:deterrent:stuck"
METRICS_CHANNEL = "scarguard:metrics:drops"

_REDIS_RECONNECT_DELAY = 5
_REDIS_MAX_RECONNECT_DELAY = 60

# v1.14 queue-overflow metric. Counts events dropped because the worker
# couldn't keep up. Bumped on every drop; published periodically so the
# web UI can surface non-zero values as a yellow flag.
_drop_counter_lock = threading.Lock()
_drop_counter = 0


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    if isinstance(cfg, dict):
        _decrypt_secrets(cfg)
    return cfg


def _decrypt_secrets(cfg: dict[str, Any]) -> None:
    """Decrypt sensitive fields (Tuya credentials) in place if a key is
    available. No-op if the secret key is absent."""
    import secret_box
    key = secret_box.try_load_key()
    if key is None:
        return
    try:
        secret_box.decrypt_in_place(cfg, key)
    except secret_box.SecretKeyMissing:
        logger.error("Failed to decrypt deterrent secrets — wrong key on disk?")


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

def _resolve_group_devices(
    group: DeterrentGroup,
    registry: list[DeviceConfig],
) -> list[DeviceConfig]:
    """Return the enabled devices referenced by *group*, preserving registry order."""
    wanted = set(group.devices)
    return [d for d in registry if d.enabled and d.name in wanted]


def _parse_event_timestamp(event: dict[str, Any]) -> float | None:
    """Return the event timestamp as unix seconds, or None if unparseable."""
    ts = event.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def _fire_group(
    group: DeterrentGroup,
    act_cfg: ActuationConfig,
    controller: TuyaCloudController,
    event: dict[str, Any],
    trigger_delay_ms: float | None,
    queue_depth: int,
    pub_holder: list[redis_lib.Redis | None],
    redis_cfg: dict[str, Any],
) -> bool:
    """Fire a single deterrent group and persist/publish the resulting event.

    Returns True if the group fired (at least one device was attempted),
    False if it was skipped (e.g. no devices resolved).
    """
    group_devices = _resolve_group_devices(group, act_cfg.devices)
    if not group_devices:
        logger.warning(
            "Group %r has no enabled devices resolvable from registry — skipping",
            group.name,
        )
        return False

    defaults = group.effective_defaults(act_cfg.defaults)
    selected, durations, inter_delays, pre_delay = build_random_plan(
        group_devices, defaults,
    )

    camera_name = event.get("camera_name", "unknown")
    class_name = event.get("class_name", "")
    confidence = event.get("confidence", 0.0)
    request_id = uuid.uuid4().hex[:16]
    logger.info(
        "Firing group [%s]: %s from %s (conf=%.2f) — %d device(s) [rid=%s]",
        group.name, class_name, camera_name, confidence, len(selected), request_id,
    )

    t_start = time.monotonic()
    actions: list[DeviceAction] = []

    if pre_delay > 0:
        logger.debug("Pre-delay: %.1fs", pre_delay)
        time.sleep(pre_delay)

    for i, device in enumerate(selected):
        if inter_delays[i] > 0:
            logger.debug("Inter-device delay: %.1fs", inter_delays[i])
            time.sleep(inter_delays[i])

        # Defence-in-depth clamp — the randomizer reads spray_duration_range
        # from config; a misconfigured or tampered config can't drive the
        # physical hold beyond MAX_ACTUATION_SEC. The controller clamps too.
        duration = clamp_duration(
            durations[i],
            max_sec=MAX_ACTUATION_SEC,
            default=3.0,
        )
        logger.info(
            "Firing device %s (%s) for %.1fs [rid=%s]",
            device.name, device.type, duration, request_id,
        )
        result = controller.activate_device(
            device, duration,
            request_id=request_id,
            event_type="detection",
        )
        actions.append(DeviceAction(
            device_name=device.name,
            device_id=device.device_id,
            device_type=device.type,
            duration_sec=duration,
            delay_before_sec=inter_delays[i],
            success=result.success,
            error=result.error,
            cloud_ack_ms=result.on_ack_ms,
            off_attempts=result.off_attempts,
            stuck=result.stuck,
        ))
        if result.stuck:
            _publish_stuck(pub_holder, redis_cfg, device, request_id, result.error or "OFF failed")

    total_duration = time.monotonic() - t_start

    actuation_event = ActuationEvent(
        timestamp=datetime.now(timezone.utc).isoformat(),
        trigger_class=class_name,
        trigger_camera=camera_name,
        trigger_confidence=confidence,
        group_name=group.name,
        pre_delay_sec=pre_delay,
        actions=actions,
        total_duration_sec=round(total_duration, 2),
        trigger_delay_ms=trigger_delay_ms,
        queue_depth=queue_depth,
        request_id=request_id,
        event_type="detection",
    )

    successes = sum(1 for a in actions if a.success)
    logger.info(
        "Group [%s] complete: %d/%d devices fired in %.1fs (trigger_delay=%s) [rid=%s]",
        group.name, successes, len(actions), total_duration,
        f"{trigger_delay_ms:.0f}ms" if trigger_delay_ms is not None else "n/a",
        request_id,
    )

    _publish_actuation(pub_holder, redis_cfg, actuation_event)
    try:
        actuation_db.insert_event(actuation_event)
    except Exception:
        logger.exception("Failed to persist actuation event")

    return True


def _publish_stuck(
    holder: list[redis_lib.Redis | None],
    redis_cfg: dict[str, Any],
    device: DeviceConfig,
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
        client = holder[0]
        if client is None:
            host = redis_cfg.get("host", "redis")
            port = int(redis_cfg.get("port", 6379))
            password = os.environ.get("REDIS_PASSWORD", "") or None
            client = redis_lib.Redis(
                host=host, port=port, password=password, decode_responses=True,
            )
            holder[0] = client
        client.publish(STUCK_CHANNEL, json.dumps(payload))
        logger.warning(
            "Published stuck event for %s (%s) [rid=%s]",
            device.name, device.device_id, request_id,
        )
    except Exception:
        logger.exception("Failed to publish stuck event for %s", device.name)
        holder[0] = None


def _worker(
    event_queue: queue.Queue[dict[str, Any] | None],
    act_cfg_ref: AtomicRef[ActuationConfig],
    controller_ref: AtomicRef[TuyaCloudController | None],
    armed_ref: AtomicRef[bool],
    cooldown: CooldownTracker,
    group_cooldown: GroupCooldownTracker,
    redis_cfg: dict[str, Any],
) -> None:
    """Consume detection events and run actuation sequences per matched group."""
    logger.info("Deterrent worker thread started")

    pub_holder: list[redis_lib.Redis | None] = [None]

    while True:
        event = event_queue.get()
        if event is None:  # poison pill — shutdown
            break

        # Latency instrumentation — dequeue moment.
        dequeue_ts = time.time()
        queue_depth = event_queue.qsize()
        event_ts = _parse_event_timestamp(event)
        trigger_delay_ms: float | None = (
            (dequeue_ts - event_ts) * 1000.0 if event_ts is not None else None
        )

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

        # Explicit-opt-in per v0.13.3: only fire groups named in matched_groups.
        # Absent/empty = no deterrent rule matched = do nothing.
        matched_groups = event.get("matched_groups") or []
        if not isinstance(matched_groups, list) or not matched_groups:
            logger.debug(
                "Event from %s has no matched deterrent groups — skipping",
                event.get("camera_name", "unknown"),
            )
            continue

        # Global cooldown gates ALL actuation (cross-group rapid-fire).
        global_cd = act_cfg.defaults.cooldown_seconds
        if not cooldown.is_clear(global_cd):
            remaining = cooldown.seconds_remaining(global_cd)
            logger.info(
                "Global cooldown active (%.0fs remaining) — skipping event",
                remaining,
            )
            continue

        # Index groups by name for quick lookup.
        groups_by_name = {g.name: g for g in act_cfg.groups}

        any_fired = False
        for group_name in matched_groups:
            group = groups_by_name.get(group_name)
            if group is None:
                logger.warning(
                    "Matched group %r not found in deterrent.groups — skipping",
                    group_name,
                )
                continue

            # Per-group cooldown gate.
            if not group_cooldown.is_clear(group_name, group.cooldown_seconds):
                remaining = group_cooldown.seconds_remaining(
                    group_name, group.cooldown_seconds,
                )
                logger.info(
                    "Group [%s] cooldown active (%.0fs remaining) — skipping group",
                    group_name, remaining,
                )
                continue

            fired = _fire_group(
                group, act_cfg, controller, event,
                trigger_delay_ms, queue_depth,
                pub_holder, redis_cfg,
            )
            if fired:
                group_cooldown.record(group_name)
                any_fired = True

        if any_fired:
            cooldown.record()

    logger.info("Deterrent worker thread stopped")


def _reconcile_loop(
    controller_ref: AtomicRef[TuyaCloudController | None],
    act_cfg_ref: AtomicRef[ActuationConfig],
    shutdown_event: threading.Event,
    redis_cfg: dict[str, Any],
    pub_holder: list[redis_lib.Redis | None],
) -> None:
    """Periodically poll every enabled device; force-OFF any that report ON
    while no activation is in flight.

    Catches two scenarios the per-activation watchdog can't:

    1. Deterrent service restarted while a device was energised — no
       watchdog thread survived the restart.
    2. The per-activation OFF succeeded from the cloud's perspective but the
       device's own state machine failed to apply it. A later status poll
       picks up the mismatch and retries.

    Runs in its own daemon thread; pacing is ``deterrent.reconcile_interval_sec``
    (default 30s, 0 to disable).
    """
    logger.info("Reconciliation loop started")

    while not shutdown_event.is_set():
        act_cfg = act_cfg_ref.get()
        interval = act_cfg.reconcile_interval_sec
        if interval <= 0:
            # Disabled — check config again in 60s in case it gets re-enabled.
            shutdown_event.wait(60)
            continue

        shutdown_event.wait(interval)
        if shutdown_event.is_set():
            break

        controller = controller_ref.get()
        if controller is None:
            continue

        act_cfg = act_cfg_ref.get()
        if not act_cfg.enabled:
            continue

        for device in act_cfg.devices:
            if not device.enabled:
                continue
            if controller.is_device_busy(device.device_id):
                continue
            switched_on = controller.is_switched_on(device)
            if switched_on is not True:
                continue

            request_id = f"reconcile-{uuid.uuid4().hex[:12]}"
            logger.critical(
                "RECONCILE — device %s (%s) reports ON with no activation — forcing OFF [rid=%s]",
                device.name, device.device_id, request_id,
            )
            ok, err = controller.force_off(device, request_id=request_id)
            if not ok:
                _publish_stuck(
                    pub_holder, redis_cfg, device, request_id,
                    err or "reconcile force_off failed",
                )

    logger.info("Reconciliation loop stopped")


def _metrics_publisher(
    redis_cfg: dict[str, Any],
    shutdown_event: threading.Event,
    interval_seconds: int = 60,
) -> None:
    """Publish queue-drop counter to Redis once per minute.

    Web UI subscribes to ``scarguard:metrics:drops`` and surfaces a
    yellow indicator when the cumulative drop count is non-zero.
    """
    holder: list[redis_lib.Redis | None] = [None]
    last_published = -1
    while not shutdown_event.wait(interval_seconds):
        with _drop_counter_lock:
            current = _drop_counter
        if current == last_published:
            continue
        last_published = current
        try:
            client = holder[0]
            if client is None:
                host = redis_cfg.get("host", "redis")
                port = int(redis_cfg.get("port", 6379))
                password = os.environ.get("REDIS_PASSWORD", "") or None
                client = redis_lib.Redis(
                    host=host, port=port, password=password,
                    decode_responses=True,
                )
                holder[0] = client
            client.publish(METRICS_CHANNEL, json.dumps({
                "service": "deterrent",
                "metric": "queue_drops_total",
                "value": current,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }))
        except Exception:
            logger.exception("Failed to publish drop metric")
            holder[0] = None


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
    """Connect to Redis and forward detection events to the worker queue.

    v1.14 verifies an HMAC signature on every event before enqueuing it.
    Because the deterrent fires physical devices, unsigned or tampered
    events are dropped silently at this layer — the detector is the sole
    authoritative source. Missing key falls back to accept-all with a loud
    warning so in-place upgrades don't brick actuation.
    """
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))
    delay = _REDIS_RECONNECT_DELAY

    hmac_key = load_key_from_env()
    if hmac_key is None:
        logger.warning(
            "DETECTION_HMAC_KEY not set — accepting unsigned detection events. "
            "Run setup.sh to generate the key and restart all services.",
        )
    else:
        logger.info("Detection event signatures will be verified")
    unsigned_warned = False
    invalid_warned = False

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

                if hmac_key is not None:
                    if not verify_event(event, hmac_key):
                        if not invalid_warned:
                            logger.error(
                                "Rejecting detection event with invalid/missing "
                                "HMAC signature — NOT firing. Camera=%s class=%s. "
                                "Further invalid events will be logged at DEBUG.",
                                event.get("camera_name"),
                                event.get("class_name"),
                            )
                            invalid_warned = True
                        else:
                            logger.debug("Invalid-signature event rejected")
                        continue
                elif not unsigned_warned:
                    unsigned_warned = True
                    logger.warning(
                        "Accepting unsigned detection event (key not set). "
                        "Further unsigned events will be logged at DEBUG.",
                    )

                logger.debug(
                    "Detection: %s from %s (conf=%.2f)",
                    event.get("class_name"),
                    event.get("camera_name"),
                    event.get("confidence", 0.0),
                )
                try:
                    event_queue.put_nowait(event)
                except queue.Full:
                    global _drop_counter
                    with _drop_counter_lock:
                        _drop_counter += 1
                    logger.warning(
                        "Event queue full — dropping event (total drops: %d)",
                        _drop_counter,
                    )

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
    start_heartbeat()

    act_cfg = parse_actuation_config(cfg)
    controller = build_controller(act_cfg)

    act_cfg_ref: AtomicRef[ActuationConfig] = AtomicRef(act_cfg)
    controller_ref: AtomicRef[TuyaCloudController | None] = AtomicRef(controller)
    armed_ref: AtomicRef[bool] = AtomicRef(cfg.get("system", {}).get("armed", True))

    cooldown = CooldownTracker()
    group_cooldown = GroupCooldownTracker()
    event_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=64)

    # Initialise actuation event database
    actuation_db.init_db()

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
        _decrypt_secrets(new_cfg)
        new_act = parse_actuation_config(new_cfg)
        new_armed = new_cfg.get("system", {}).get("armed", True)

        # Rebuild controller if credentials changed
        old_act = act_cfg_ref.get()
        if new_act.tuya != old_act.tuya:
            new_controller = build_controller(new_act)
            controller_ref.set(new_controller)
            logger.info("Tuya Cloud controller rebuilt (credentials changed)")

            # Create or update battery monitor with new controller
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
            elif new_controller is not None and battery_monitor is not None:
                battery_monitor.update_controller(new_controller)
                logger.info("Battery monitor controller updated (credentials changed)")

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
        args=(
            event_queue, act_cfg_ref, controller_ref, armed_ref,
            cooldown, group_cooldown, redis_cfg,
        ),
    )
    worker_thread.start()

    # Start request handler (test-fire + device status queries from web UI)
    req_handler = RequestHandler(redis_cfg, act_cfg_ref, controller_ref)
    req_handler.start()

    # Start reconciliation loop (force-OFF stuck devices)
    reconcile_pub_holder: list[redis_lib.Redis | None] = [None]
    reconcile_thread = threading.Thread(
        target=_reconcile_loop,
        name="deterrent-reconcile",
        daemon=True,
        args=(
            controller_ref, act_cfg_ref, shutdown_event,
            redis_cfg, reconcile_pub_holder,
        ),
    )
    reconcile_thread.start()

    # Start queue-drop metrics publisher
    metrics_thread = threading.Thread(
        target=_metrics_publisher,
        name="deterrent-metrics",
        daemon=True,
        args=(redis_cfg, shutdown_event),
    )
    metrics_thread.start()

    # Subscribe loop blocks until shutdown
    subscribe_loop(redis_cfg, event_queue, shutdown_event)

    # Cleanup
    event_queue.put(None)  # ensure worker exits
    worker_thread.join(timeout=10)
    reconcile_thread.join(timeout=10)
    req_handler.stop()
    watcher.stop()
    if battery_monitor is not None:
        battery_monitor.stop()

    logger.info("Deterrent service stopped cleanly")


if __name__ == "__main__":
    main()
