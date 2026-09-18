"""ScarGuard deterrent - subscribes to Redis detections and triggers Tuya devices."""

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
    DeviceConfig,
)
from atomic_ref import AtomicRef
from battery_monitor import BatteryMonitor
from cloud_controller import TuyaCloudController
from config_watcher import ConfigWatcher
from cooldown import CooldownTracker, GroupCooldownTracker
from deterrent_safety import MAX_GROUP_TEST_FIRE_SEC
from event_signing import load_key_from_env, verify_event
from group_fire import execute_plan, resolve_group_devices
from healthcheck import start_heartbeat
from request_handler import JOB_TEST_FIRE_GROUP, InFlightGuard, RequestHandler

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
        logger.error("Failed to decrypt deterrent secrets - wrong key on disk?")


def setup_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
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
# Worker thread - processes events sequentially (tinytuya.Cloud isn't
# documented as thread-safe, and actuation sequences are inherently serial).
# ---------------------------------------------------------------------------

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
    group_devices = resolve_group_devices(group, act_cfg.devices)
    if not group_devices:
        logger.warning(
            "Group %r has no enabled devices resolvable from registry - skipping",
            group.name,
        )
        return False

    defaults = group.effective_defaults(act_cfg.defaults)

    camera_name = event.get("camera_name", "unknown")
    class_name = event.get("class_name", "")
    confidence = event.get("confidence", 0.0)
    request_id = uuid.uuid4().hex[:16]
    logger.info(
        "Firing group [%s]: %s from %s (conf=%.2f) - %d eligible device(s) [rid=%s]",
        group.name, class_name, camera_name, confidence, len(group_devices), request_id,
    )

    execution = execute_plan(
        controller,
        group_devices,
        defaults,
        request_id=request_id,
        event_type="detection",
        label=f"Group [{group.name}]",
        on_stuck=lambda device, error: _publish_stuck(
            pub_holder, redis_cfg, device, request_id, error,
        ),
    )
    actions = execution.actions
    pre_delay = execution.pre_delay_sec

    total_duration = execution.total_duration_sec

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

    successes = execution.successes
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


def _run_group_test_fire(
    job: dict[str, Any],
    act_cfg_ref: AtomicRef[ActuationConfig],
    controller_ref: AtomicRef[TuyaCloudController | None],
    armed_ref: AtomicRef[bool],
    cooldown: CooldownTracker,
    group_cooldown: GroupCooldownTracker,
    pub_holder: list[redis_lib.Redis | None],
    redis_cfg: dict[str, Any],
) -> None:
    """Run an admin group test-fire on the worker thread.

    Deliberately applies the same gates a detection does. An operator who has
    set ``deterrent.enabled: false`` or disarmed the system to work on the pond
    must not be able to start the sprinklers from a button, and a test-fire is
    the same physical event as a detection so it consumes the same cooldown
    budget rather than letting a real heron re-fire the group a second later.
    """
    request_id = job.get("request_id", "")
    group_name = job.get("group_name", "")
    result_channel = job.get("result_channel", "")

    def reply(body: dict[str, Any]) -> None:
        _publish_raw(pub_holder, redis_cfg, result_channel, body)

    act_cfg = act_cfg_ref.get()
    controller = controller_ref.get()

    if not act_cfg.enabled:
        reply({"ok": False, "error": "Deterrent is disabled in config"})
        return
    if not armed_ref.get():
        reply({"ok": False, "error": "System is disarmed"})
        return
    if controller is None:
        reply({"ok": False, "error": "No Tuya credentials configured"})
        return

    group = next((g for g in act_cfg.groups if g.name == group_name), None)
    if group is None:
        reply({"ok": False, "error": f"Group {group_name} not found in config"})
        return

    global_cd = act_cfg.defaults.cooldown_seconds
    if not cooldown.is_clear(global_cd):
        reply({
            "ok": False,
            "error": (
                f"Global cooldown active, {cooldown.seconds_remaining(global_cd):.0f}s remaining"
            ),
        })
        return
    if not group_cooldown.is_clear(group_name, group.cooldown_seconds):
        remaining = group_cooldown.seconds_remaining(group_name, group.cooldown_seconds)
        reply({
            "ok": False,
            "error": f"Group cooldown active, {remaining:.0f}s remaining",
        })
        return

    group_devices = resolve_group_devices(group, act_cfg.devices)
    if not group_devices:
        reply({"ok": False, "error": f"Group {group_name} has no enabled devices"})
        return

    logger.info(
        "Test-fire group [%s]: %d eligible device(s) [rid=%s]",
        group.name, len(group_devices), request_id,
    )
    execution = execute_plan(
            controller,
        group_devices,
        group.effective_defaults(act_cfg.defaults),
        request_id=request_id,
        event_type="test_fire_group",
        label=f"Test-fire group [{group.name}]",
        deadline_sec=MAX_GROUP_TEST_FIRE_SEC,
        on_stuck=lambda device, error: _publish_stuck(
            pub_holder, redis_cfg, device, request_id, error,
        ),
    )

    if not execution.actions:
        # Nothing physical happened, so do not burn a cooldown the operator
        # would then be locked out by, and say why rather than returning a
        # bare failure the web route turns into an unexplained 502.
        reply({
            "ok": False,
            "error": "No device fired: the firing window elapsed before any could start",
            "group_name": group.name,
            "devices_fired": 0,
            "devices_succeeded": 0,
            "devices": [],
        })
        return

    group_cooldown.record(group_name)
    cooldown.record()

    actuation_event = ActuationEvent(
        timestamp=datetime.now(timezone.utc).isoformat(),
        trigger_class="admin",
        trigger_camera="test-fire-group",
        trigger_confidence=0.0,
        group_name=group.name,
        pre_delay_sec=execution.pre_delay_sec,
        actions=execution.actions,
        total_duration_sec=round(execution.total_duration_sec, 2),
        request_id=request_id,
        event_type="test_fire_group",
    )
    _publish_actuation(pub_holder, redis_cfg, actuation_event)
    try:
        actuation_db.insert_event(actuation_event)
    except Exception:
        logger.exception("Failed to persist group test-fire [rid=%s]", request_id)

    # Partial success is not failure: report the counts and let the operator
    # judge. "ok" means at least one device did what was asked.
    reply({
        "ok": execution.successes > 0,
        "group_name": group.name,
        "devices_fired": len(execution.actions),
        "devices_succeeded": execution.successes,
        "total_duration_sec": round(execution.total_duration_sec, 2),
        "devices": [
            {
                "device_name": a.device_name,
                "duration_sec": a.duration_sec,
                "success": a.success,
                "error": a.error,
                "stuck": a.stuck,
            }
            for a in execution.actions
        ],
    })
    logger.info(
        "Test-fire group [%s] complete: %d/%d devices in %.1fs [rid=%s]",
        group.name, execution.successes, len(execution.actions),
        execution.total_duration_sec, request_id,
    )


def _publish_raw(
    holder: list[redis_lib.Redis | None],
    redis_cfg: dict[str, Any],
    channel: str,
    body: dict[str, Any],
) -> None:
    """Publish a plain dict to *channel*.  Lazily connects, mirrors _publish_actuation."""
    if not channel:
        return
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
        client.publish(channel, json.dumps(body))
    except Exception:
        logger.exception("Failed to publish to %s", channel)
        holder[0] = None  # force reconnect on next attempt


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
    in_flight: InFlightGuard,
) -> None:
    """Consume detection events and run actuation sequences per matched group."""
    logger.info("Deterrent worker thread started")

    pub_holder: list[redis_lib.Redis | None] = [None]

    while True:
        event = event_queue.get()
        if event is None:  # poison pill - shutdown
            break

        # Control jobs ride the same queue so they serialise with detection
        # firing: two sequences can never overlap on one device, which is what
        # keeps the reconcile loop's busy check meaningful.
        if event.get("__job") == JOB_TEST_FIRE_GROUP:
            # A raise here would kill this thread and with it every
            # detection-driven actuation, while the healthcheck kept reporting
            # the container healthy. The pond would be unprotected and the only
            # symptom would be a 502 on the admin page blaming the wrong thing.
            try:
                _run_group_test_fire(
                    event, act_cfg_ref, controller_ref, armed_ref,
                    cooldown, group_cooldown, pub_holder, redis_cfg,
                )
            except Exception:
                logger.exception(
                    "Group test-fire raised [rid=%s]", event.get("request_id", ""),
                )
                _publish_raw(
                    pub_holder, redis_cfg, event.get("result_channel", ""),
                    {"ok": False, "error": "Group test-fire failed, see deterrent logs"},
                )
            finally:
                in_flight.release()
            continue

        # Latency instrumentation - dequeue moment.
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
            logger.debug("Actuation disabled - ignoring event")
            continue
        if not armed_ref.get():
            logger.debug("System disarmed - ignoring event")
            continue
        if controller is None:
            logger.warning("No Tuya credentials configured - cannot actuate")
            continue

        # Explicit-opt-in per v0.13.3: only fire groups named in matched_groups.
        # Absent/empty = no deterrent rule matched = do nothing.
        matched_groups = event.get("matched_groups") or []
        if not isinstance(matched_groups, list) or not matched_groups:
            logger.debug(
                "Event from %s has no matched deterrent groups - skipping",
                event.get("camera_name", "unknown"),
            )
            continue

        # Global cooldown gates ALL actuation (cross-group rapid-fire).
        global_cd = act_cfg.defaults.cooldown_seconds
        if not cooldown.is_clear(global_cd):
            remaining = cooldown.seconds_remaining(global_cd)
            logger.info(
                "Global cooldown active (%.0fs remaining) - skipping event",
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
                    "Matched group %r not found in deterrent.groups - skipping",
                    group_name,
                )
                continue

            # Per-group cooldown gate.
            if not group_cooldown.is_clear(group_name, group.cooldown_seconds):
                remaining = group_cooldown.seconds_remaining(
                    group_name, group.cooldown_seconds,
                )
                logger.info(
                    "Group [%s] cooldown active (%.0fs remaining) - skipping group",
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

    1. Deterrent service restarted while a device was energised - no
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
            # Disabled - check config again in 60s in case it gets re-enabled.
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
                "RECONCILE - device %s (%s) reports ON with no activation - forcing OFF [rid=%s]",
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
# Subscribe loop - mirrors the notifier pattern
# ---------------------------------------------------------------------------

def subscribe_loop(
    redis_cfg: dict[str, Any],
    event_queue: queue.Queue[dict[str, Any] | None],
    shutdown_event: threading.Event,
) -> None:
    """Connect to Redis and forward detection events to the worker queue.

    v1.14 verifies an HMAC signature on every event before enqueuing it.
    Because the deterrent fires physical devices, unsigned or tampered
    events are dropped silently at this layer - the detector is the sole
    authoritative source. Missing key falls back to accept-all with a loud
    warning so in-place upgrades don't brick actuation.
    """
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))
    delay = _REDIS_RECONNECT_DELAY

    hmac_key = load_key_from_env()
    if hmac_key is None:
        logger.warning(
            "DETECTION_HMAC_KEY not set - accepting unsigned detection events. "
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
                                "HMAC signature - NOT firing. Camera=%s class=%s. "
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
                        "Event queue full - dropping event (total drops: %d)",
                        _drop_counter,
                    )

        except redis_lib.RedisError:
            if shutdown_event.is_set():
                break
            logger.exception("Redis connection lost - retrying in %ds", delay)
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
        logger.info("Actuation disabled in config - service will idle until enabled")
    elif controller is None:
        logger.warning("Actuation enabled but Tuya credentials missing - check config")
    else:
        enabled_count = sum(1 for d in act_cfg.devices if d.enabled)
        logger.info(
            "Actuation enabled - %d device(s) registered, cooldown %ds",
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
        logger.info("Received signal %s - shutting down", sig)
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
            "Config reloaded - actuation %s, %d device(s), armed=%s",
            "enabled" if new_act.enabled else "disabled",
            enabled_count,
            new_armed,
        )

    watcher = ConfigWatcher(CONFIG_PATH, _on_config_change)
    watcher.start()

    # Claimed by the request handler before it enqueues and released by the
    # worker when the sequence ends, so a second press is refused for the whole
    # queued-and-running window rather than stacking behind live hardware.
    in_flight = InFlightGuard()

    # Start worker thread
    worker_thread = threading.Thread(
        target=_worker,
        name="deterrent-worker",
        daemon=True,
        args=(
            event_queue, act_cfg_ref, controller_ref, armed_ref,
            cooldown, group_cooldown, redis_cfg, in_flight,
        ),
    )
    worker_thread.start()

    # Start request handler (test-fire + device status queries from web UI).
    # Group test-fires are handed to the worker queue rather than run on the
    # handler thread, which must stay free to answer emergency force-off.
    req_handler = RequestHandler(
        redis_cfg, act_cfg_ref, controller_ref,
        job_queue=event_queue, in_flight=in_flight,
    )
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
