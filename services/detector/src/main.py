"""ScarGuard detector — main loop.

Reads frames from one or more RTSP streams concurrently, runs YOLO inference,
applies cooldown deduplication, persists events to SQLite, and publishes to Redis.

Each enabled camera runs in its own thread.  Cameras that share the same model
share a single YOLODetector instance (managed by ModelPool), while cameras with
different models can run inference concurrently on separate GPU locks.

The EventProcessor is shared across threads and is internally thread-safe.
Each camera thread owns its own RTSPStream and RedisPublisher connection.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml
from atomic_ref import AtomicRef
from camera_health import CameraHealthTracker
from cleanup import RetentionCleaner
from config_watcher import ConfigWatcher
from detector import YOLODetector
from evaluator import EvaluationRunner
from events import EventProcessor
from metrics_store import MetricsStore
from model_pool import ModelPool
from publisher import RedisPublisher
from scheduler import ArmScheduler
from snapshot_grabber import SnapshotGrabber
from stats_collector import StatsCollector
from stream import RTSPStream
from visit_tracker import VisitTracker

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/scarguard.yml")
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/data/snapshots")
DB_PATH = os.environ.get("DB_PATH", "/data/scarguard.db")


@dataclass
class CameraState:
    """Bookkeeping for a running camera thread."""

    thread: threading.Thread
    stop_event: threading.Event
    zones_ref: AtomicRef[list[dict]]
    rules_ref: AtomicRef[list[dict]]
    model_path: str | None
    target_classes: set[str] | None
    detector: YOLODetector


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def setup_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,
    )


def _match_action_rules(class_name: str, rules: list[dict]) -> list[str] | None:
    """Return channel names for the first matching action rule.

    Rules are evaluated in order; the first rule whose class_name matches
    (or is the wildcard "*") wins.  Returns ``None`` when no rule matches,
    signalling that notifications should be suppressed for this class.
    """
    for rule in rules:
        rule_class = rule.get("class_name", "*")
        if rule_class == "*" or rule_class == class_name:
            return list(rule.get("channels", []))
    return None


def _in_exclusion_zone(
    cx: int,
    cy: int,
    frame_w: int,
    frame_h: int,
    zones: list[dict],
) -> bool:
    """Return True if the normalized detection center is inside any exclusion zone."""
    if not zones or frame_w == 0 or frame_h == 0:
        return False
    cx_f = cx / frame_w
    cy_f = cy / frame_h
    for zone in zones:
        zx = float(zone.get("x", 0))
        zy = float(zone.get("y", 0))
        zw = float(zone.get("w", 0))
        zh = float(zone.get("h", 0))
        if zx <= cx_f <= zx + zw and zy <= cy_f <= zy + zh:
            return True
    return False


def _resolve_known_channels(cfg: dict) -> set[str]:
    """Collect all defined notification channel names from config."""
    channels: set[str] = set()
    notif = cfg.get("notifications", {})
    for ch in notif.get("channels", []):
        if isinstance(ch, dict) and ch.get("name"):
            channels.add(ch["name"])
    # Legacy flat sections count as implicit channels.
    if notif.get("discord", {}).get("enabled"):
        channels.add("discord")
    if notif.get("email", {}).get("enabled"):
        channels.add("email")
    return channels


def _validate_action_rules(
    camera_name: str,
    rules: list[dict],
    known_channels: set[str],
) -> None:
    """Log warnings for action rules that reference undefined channels."""
    for rule in rules:
        for ch_name in rule.get("channels", []):
            if ch_name not in known_channels:
                logger.warning(
                    "[%s] action_rules references unknown channel: %s",
                    camera_name,
                    ch_name,
                )


def _apply_exclusion_zones(
    detections: list,
    zones: list[dict],
    frame_w: int,
    frame_h: int,
    camera_name: str,
) -> list:
    """Filter detections that fall inside exclusion zones."""
    if not zones:
        return detections
    filtered = [
        det for det in detections
        if not _in_exclusion_zone(
            (det.bbox[0] + det.bbox[2]) // 2,
            (det.bbox[1] + det.bbox[3]) // 2,
            frame_w, frame_h, zones,
        )
    ]
    if not filtered and detections:
        logger.debug("[%s] All detections suppressed by exclusion zones", camera_name)
    return filtered


def _evaluate_action_rules(
    detections: list,
    rules: list[dict],
) -> dict[str, list[str] | None]:
    """Build a class_name → channel list mapping from action rules."""
    actions_by_class: dict[str, list[str] | None] = {}
    if not rules:
        return actions_by_class
    for det in detections:
        if det.class_name not in actions_by_class:
            actions_by_class[det.class_name] = _match_action_rules(
                det.class_name, rules
            )
    return actions_by_class


def _publish_detections(
    detections: list,
    camera_name: str,
    frame: object,
    event_processor: EventProcessor,
    publisher: RedisPublisher,
    visit_tracker: VisitTracker | None,
    actions_by_class: dict[str, list[str] | None],
) -> None:
    """Run cooldown dedup, publish events to Redis, and record visits."""
    events = event_processor.process(
        detections, camera_name, frame,
        actions_by_class=actions_by_class if actions_by_class else None,
    )
    for event in events:
        publisher.publish(event)
    if visit_tracker is not None:
        for event in events:
            visit_tracker.record_detection(
                camera_name=camera_name,
                class_name=event["class_name"],
                timestamp=datetime.fromisoformat(event["timestamp"]),
            )


def run_camera(
    camera_cfg: dict,
    detector: YOLODetector,
    target_classes: set[str] | None,
    event_processor: EventProcessor,
    redis_cfg: dict,
    frame_skip_ref: AtomicRef[int],
    armed_ref: AtomicRef[bool],
    exclusion_zones_ref: AtomicRef[list[dict]],
    action_rules_ref: AtomicRef[list[dict]],
    stop_event: threading.Event,
    camera_stats: dict[str, dict] | None = None,
    camera_stats_lock: threading.Lock | None = None,
    health_tracker: CameraHealthTracker | None = None,
    visit_tracker: VisitTracker | None = None,
) -> None:
    """Per-camera detection loop — runs in its own thread.

    Each thread owns its RTSPStream (with independent reconnect backoff) and
    a dedicated RedisPublisher connection.  Stopping the thread is done by
    setting *stop_event*.
    """
    name = camera_cfg["name"]

    redis_password = os.environ.get("REDIS_PASSWORD", "")
    publisher = RedisPublisher(
        host=redis_cfg.get("host", "redis"),
        port=int(redis_cfg.get("port", 6379)),
        password=redis_password or None,
    )
    stream = RTSPStream(name=name, rtsp_url=camera_cfg["rtsp_url"], stop_event=stop_event)

    logger.info(
        "[%s] Camera thread starting | frame_skip=%d | model=%s | classes=%s",
        name,
        frame_skip_ref.get(),
        detector.model_path,
        sorted(target_classes) if target_classes else "(global)",
    )

    frame_count = 0
    # Per-camera inference tracking for stats
    _infer_count = 0
    _infer_total_ms = 0.0
    _infer_window_start = time.monotonic()

    while not stop_event.is_set():
        frame_count += 1
        if frame_count % frame_skip_ref.get() != 0:
            # Advance stream without decoding — saves CPU/GPU on skipped frames
            if not stream.grab():
                if health_tracker is not None:
                    health_tracker.record_failure(name)
                stop_event.wait(1.0)
            continue

        ret, frame = stream.read()
        if not ret:
            if health_tracker is not None:
                health_tracker.record_failure(name)
            # RTSPStream handles its own backoff; wait briefly so we don't
            # spin-check stop_event too aggressively, but wake up immediately
            # on shutdown rather than blocking for a full second.
            stop_event.wait(1.0)
            continue

        # Touch health marker so Docker health check knows we're alive
        Path("/tmp/healthy").touch(exist_ok=True)

        if health_tracker is not None:
            health_tracker.record_frame(name)

        if not armed_ref.get():
            continue

        t0 = time.monotonic()
        detections = detector.predict(frame, target_classes=target_classes)
        infer_ms = (time.monotonic() - t0) * 1000.0
        _infer_count += 1
        _infer_total_ms += infer_ms

        # Update shared camera stats; reset window every 30s for recent-only metrics
        if camera_stats is not None and camera_stats_lock is not None:
            elapsed = time.monotonic() - _infer_window_start
            fps = _infer_count / elapsed if elapsed > 0 else 0.0
            avg_ms = _infer_total_ms / _infer_count if _infer_count > 0 else 0.0
            with camera_stats_lock:
                camera_stats[name] = {
                    "fps": round(fps, 1),
                    "avg_inference_ms": round(avg_ms, 1),
                }
            if elapsed > 30.0:
                _infer_count = 0
                _infer_total_ms = 0.0
                _infer_window_start = time.monotonic()

        if not detections:
            continue

        # Exclusion zones are applied BEFORE cooldown dedup intentionally:
        # an object permanently in an excluded region should not consume the
        # cooldown slot for its class on this camera.
        frame_h, frame_w = frame.shape[:2]
        detections = _apply_exclusion_zones(
            detections, exclusion_zones_ref.get(), frame_w, frame_h, name,
        )
        if not detections:
            continue

        actions_by_class = _evaluate_action_rules(
            detections, action_rules_ref.get(),
        )
        _publish_detections(
            detections, name, frame,
            event_processor, publisher, visit_tracker,
            actions_by_class,
        )

    stream.release()
    logger.info("[%s] Camera thread stopped", name)


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.get("system", {}).get("log_level", "info"))
    logger.info("ScarGuard detector starting")
    Path("/tmp/healthy").touch(exist_ok=True)

    # ---- Enabled cameras ------------------------------------------------------
    cameras: list[dict] = [c for c in cfg.get("cameras", []) if c.get("enabled", True)]
    if not cameras:
        logger.error("No enabled cameras found in config — exiting")
        sys.exit(1)

    # ---- Config sections -------------------------------------------------------
    sys_cfg: dict = cfg.get("system", {})
    det_cfg: dict = cfg.get("detection", {})
    redis_cfg: dict = cfg.get("redis", {})
    # Mutable references so hot-reload can update these without restarting threads.
    armed_ref: AtomicRef[bool] = AtomicRef(sys_cfg.get("armed", True))
    frame_skip_ref: AtomicRef[int] = AtomicRef(det_cfg.get("frame_skip", 2))

    # ---- Model pool ------------------------------------------------------------
    model_pool = ModelPool(
        default_model_path=det_cfg["model_path"],
        default_confidence=det_cfg.get("confidence_threshold", 0.25),
        default_classes=det_cfg.get("target_classes", []),
    )

    event_processor = EventProcessor(
        cooldown_seconds=det_cfg.get("cooldown_seconds", 30),
        snapshot_dir=SNAPSHOT_DIR,
        db_path=DB_PATH,
    )

    # Unified retention_days; fall back to legacy snapshot_retention_days for
    # configs that haven't been migrated yet by the web service.
    _ret = sys_cfg.get("retention_days")
    if _ret is None:
        _ret = sys_cfg.get("snapshot_retention_days")
    if _ret is None:
        _ret = 90
    retention_days = int(_ret)
    cleaner = RetentionCleaner(
        snapshot_dir=SNAPSHOT_DIR,
        db_path=DB_PATH,
        retention_days=retention_days,
    )
    cleaner.start()

    # ---- Camera health tracking ---------------------------------------------------
    health_cfg = sys_cfg.get("camera_health", {})
    health_tracker = CameraHealthTracker(
        alert_threshold_seconds=int(health_cfg.get("alert_threshold_minutes", 10)) * 60,
        debounce_seconds=int(health_cfg.get("debounce_seconds", 30)),
    )

    # ---- Arm/disarm scheduler --------------------------------------------------
    _config_write_lock = threading.Lock()

    def _write_armed_to_config(armed: bool) -> None:
        """Persist a scheduler-triggered arm/disarm change to scarguard.yml.

        Uses an explicit lock to prevent concurrent YAML read-modify-write from
        the scheduler thread racing against other in-process config mutations.
        """
        with _config_write_lock:
            try:
                with open(CONFIG_PATH) as f:
                    file_cfg = yaml.safe_load(f) or {}
                file_cfg.setdefault("system", {})["armed"] = armed
                with open(CONFIG_PATH, "w") as f:
                    yaml.dump(file_cfg, f, default_flow_style=False, sort_keys=False)
            except Exception:
                logger.exception("Failed to write armed=%s to config file", armed)

    def _on_scheduler_transition(armed: bool) -> None:
        _write_armed_to_config(armed)
        event_processor.log_system_event("armed" if armed else "disarmed")

    def _make_redis():  # type: ignore[return]
        import redis

        _pw = os.environ.get("REDIS_PASSWORD", "") or None
        return redis.Redis(
            host=redis_cfg.get("host", "redis"),
            port=int(redis_cfg.get("port", 6379)),
            password=_pw,
            decode_responses=True,
        )

    scheduler = ArmScheduler(armed_ref, _on_scheduler_transition, get_redis=_make_redis)
    scheduler.configure(sys_cfg.get("schedule", {}), sys_cfg.get("timezone", "UTC"))
    scheduler.start()

    # ---- Metrics store -----------------------------------------------------------
    metrics_store = MetricsStore(db_path=DB_PATH)

    # ---- Visit tracker -----------------------------------------------------------
    visit_timeout = int(sys_cfg.get("visit_timeout_seconds", 300))
    visit_tracker = VisitTracker(db_path=DB_PATH, timeout_seconds=visit_timeout)

    # ---- Signal handling -------------------------------------------------------
    global_stop = threading.Event()

    def _shutdown(sig: int, _frame: object) -> None:
        logger.info("Received signal %s — shutting down", sig)
        global_stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # ---- Channel validation at startup ----------------------------------------
    known_channels = _resolve_known_channels(cfg)

    # ---- Per-camera thread management -----------------------------------------
    active_cameras: dict[str, CameraState] = {}
    camera_stats: dict[str, dict] = {}
    camera_stats_lock = threading.Lock()

    def _start_camera(camera_cfg: dict) -> bool:
        """Start a camera thread. Returns False if the camera was skipped."""
        name = camera_cfg["name"]

        # Resolve per-camera model (None → global default via pool).
        cam_model_path: str | None = camera_cfg.get("model_path") or None
        effective_model = cam_model_path or model_pool._default_model_path

        # Validate model exists on disk.
        if not ModelPool.validate_model_exists(effective_model):
            logger.error(
                "[%s] Model %s not found — skipping camera",
                name,
                effective_model,
            )
            return False

        # Validate action rule channel references.
        _validate_action_rules(
            name,
            camera_cfg.get("action_rules", []),
            known_channels,
        )

        # Resolve per-camera target classes (None → use model/global default).
        cam_classes_raw: list[str] | None = camera_cfg.get("detect_classes")
        cam_classes: set[str] | None = set(cam_classes_raw) if cam_classes_raw is not None else None

        try:
            cam_detector = model_pool.get_detector(cam_model_path)
        except Exception:
            logger.error("[%s] Failed to load model %s — skipping camera", name, effective_model)
            return False

        cam_stop = threading.Event()
        zones_ref: AtomicRef[list[dict]] = AtomicRef(list(camera_cfg.get("exclusion_zones", [])))
        rules_ref: AtomicRef[list[dict]] = AtomicRef(list(camera_cfg.get("action_rules", [])))
        t = threading.Thread(
            target=run_camera,
            args=(
                camera_cfg,
                cam_detector,
                cam_classes,
                event_processor,
                redis_cfg,
                frame_skip_ref,
                armed_ref,
                zones_ref,
                rules_ref,
                cam_stop,
                camera_stats,
                camera_stats_lock,
                health_tracker,
                visit_tracker,
            ),
            name=f"camera-{name}",
            daemon=True,
        )
        t.start()
        active_cameras[name] = CameraState(
            thread=t,
            stop_event=cam_stop,
            zones_ref=zones_ref,
            rules_ref=rules_ref,
            model_path=effective_model,
            target_classes=cam_classes,
            detector=cam_detector,
        )
        logger.info("[%s] Camera thread started", name)
        return True

    def _stop_camera(name: str) -> None:
        if name not in active_cameras:
            return
        state = active_cameras.pop(name)
        state.stop_event.set()
        state.thread.join(timeout=5)
        model_pool.release(state.model_path)
        health_tracker.remove_camera(name)
        with camera_stats_lock:
            camera_stats.pop(name, None)
        logger.info("[%s] Camera thread stopped", name)

    for camera_cfg in cameras:
        _start_camera(camera_cfg)

    started = list(active_cameras.keys())
    logger.info(
        "Monitoring %d camera(s): %s | armed=%s | cooldown=%ds",
        len(started),
        ", ".join(started),
        armed_ref.get(),
        det_cfg.get("cooldown_seconds", 30),
    )

    # ---- Stats collector -------------------------------------------------------
    stats_interval = int(sys_cfg.get("stats_interval", 5))
    stats_collector = StatsCollector(
        redis_cfg=redis_cfg,
        interval_seconds=stats_interval,
        camera_stats=camera_stats,
        camera_stats_lock=camera_stats_lock,
        stop_event=global_stop,
        health_tracker=health_tracker,
        metrics_store=metrics_store,
    )
    stats_collector.start()

    # ---- Visit flush thread -------------------------------------------------------
    def _visit_flush_loop() -> None:
        while not global_stop.wait(60):
            try:
                visit_tracker.flush_expired()
            except Exception:
                logger.exception("Visit flush error")

    visit_flush_thread = threading.Thread(
        target=_visit_flush_loop, name="visit-flush", daemon=True
    )
    visit_flush_thread.start()

    # ---- Model evaluation runner -----------------------------------------------
    eval_runner = EvaluationRunner(
        redis_cfg=redis_cfg,
        db_path=DB_PATH,
        snapshot_dir=SNAPSHOT_DIR,
    )
    eval_runner.start()

    # ---- Snapshot grabber -------------------------------------------------------
    snapshot_grabber = SnapshotGrabber(
        redis_cfg=redis_cfg,
        cameras_cfg=cameras,
        stop_event=global_stop,
    )
    snapshot_grabber.start()

    # ---- Config hot-reload ----------------------------------------------------
    def _on_config_change(new_cfg: dict) -> None:
        nonlocal known_channels
        new_sys = new_cfg.get("system", {})
        new_det = new_cfg.get("detection", {})
        new_cameras_list: list[dict] = [
            c for c in new_cfg.get("cameras", []) if c.get("enabled", True)
        ]
        new_camera_names = {c["name"] for c in new_cameras_list}
        old_camera_names = set(active_cameras.keys())

        changes: list[str] = []

        # armed flag
        new_armed = new_sys.get("armed", True)
        if new_armed != armed_ref.get():
            armed_ref.set(new_armed)
            changes.append(f"armed={new_armed}")

        # Update global defaults in the model pool (confidence and target_classes
        # propagate to all loaded detectors for immediate effect).
        new_global_classes = new_det.get("target_classes", [])
        old_global_classes = model_pool._default_classes  # snapshot before update
        model_pool.update_defaults(
            new_det.get("model_path", model_pool._default_model_path),
            new_det.get("confidence_threshold", 0.25),
            new_global_classes,
        )

        # If global classes changed, restart cameras that rely on the global
        # default (target_classes is None) so they pick up the new filter.
        if set(new_global_classes) != set(old_global_classes):
            for cam_name, state in list(active_cameras.items()):
                if state.target_classes is None and cam_name in new_camera_names:
                    cam_cfg_match = next(
                        (c for c in new_cameras_list if c["name"] == cam_name), None
                    )
                    if cam_cfg_match:
                        _stop_camera(cam_name)
                        if _start_camera(cam_cfg_match):
                            changes.append(f"global classes updated: {cam_name}")

        new_cooldown = new_det.get("cooldown_seconds", 30)
        if new_cooldown != event_processor.cooldown_seconds:
            event_processor.cooldown_seconds = new_cooldown
            changes.append(f"cooldown_seconds={new_cooldown}")

        new_frame_skip = new_det.get("frame_skip", 2)
        if new_frame_skip != frame_skip_ref.get():
            frame_skip_ref.set(new_frame_skip)
            changes.append(f"frame_skip={new_frame_skip}")

        # Refresh known channels for validation.
        known_channels = _resolve_known_channels(new_cfg)

        # Update snapshot grabber camera list
        snapshot_grabber.update_cameras(new_cameras_list)

        # cameras added / updated
        for cam_cfg in new_cameras_list:
            cam_name = cam_cfg["name"]
            if cam_name not in old_camera_names:
                if _start_camera(cam_cfg):
                    changes.append(f"camera added: {cam_name}")
                else:
                    changes.append(f"camera skipped (bad model): {cam_name}")
            else:
                state = active_cameras[cam_name]

                # Resolve new per-camera model/classes.
                new_model_raw = cam_cfg.get("model_path") or None
                new_effective_model = new_model_raw or model_pool._default_model_path
                new_classes_raw = cam_cfg.get("detect_classes")
                new_classes = set(new_classes_raw) if new_classes_raw is not None else None

                # If model or classes changed, restart the camera thread so it
                # picks up the new detector / class filter.
                if new_effective_model != state.model_path or new_classes != state.target_classes:
                    _stop_camera(cam_name)
                    if _start_camera(cam_cfg):
                        changes.append(f"model/classes updated: {cam_name}")
                    else:
                        changes.append(f"camera skipped after update (bad model): {cam_name}")
                else:
                    # Hot-reload exclusion zones and action rules for running cameras.
                    new_zones = list(cam_cfg.get("exclusion_zones", []))
                    if new_zones != state.zones_ref.get():
                        state.zones_ref.set(new_zones)
                        changes.append(f"exclusion_zones updated: {cam_name}")
                    new_rules = list(cam_cfg.get("action_rules", []))
                    if new_rules != state.rules_ref.get():
                        _validate_action_rules(cam_name, new_rules, known_channels)
                        state.rules_ref.set(new_rules)
                        changes.append(f"action_rules updated: {cam_name}")

        # cameras removed or disabled
        for name in old_camera_names - new_camera_names:
            _stop_camera(name)
            changes.append(f"camera removed: {name}")

        # schedule config
        new_schedule = new_sys.get("schedule", {})
        new_tz = new_sys.get("timezone", "UTC")
        scheduler.configure(new_schedule, new_tz)

        if changes:
            logger.info("Config reloaded — changes: %s", ", ".join(changes))
        else:
            logger.info("Config reloaded — no effective changes")

    watcher = ConfigWatcher(CONFIG_PATH, _on_config_change)
    watcher.start()

    # ---- Wait for shutdown -----------------------------------------------------
    global_stop.wait()

    watcher.stop()
    scheduler.stop()
    eval_runner.stop()
    for name in list(active_cameras.keys()):
        _stop_camera(name)
    # snapshot_grabber stops via global_stop event (daemon thread)
    visit_tracker.flush_all()
    event_processor.close()
    cleaner.stop()

    logger.info("Detector stopped cleanly")


if __name__ == "__main__":
    main()
