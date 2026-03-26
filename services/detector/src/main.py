"""ScarGuard detector — main loop.

Reads frames from one or more RTSP streams concurrently, runs YOLO inference,
applies cooldown deduplication, persists events to SQLite, and publishes to Redis.

Each enabled camera runs in its own thread.  The YOLODetector and EventProcessor
are shared across threads; both are internally thread-safe.  Each camera thread
owns its own RTSPStream and RedisPublisher connection.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

import yaml
from cleanup import SnapshotCleaner
from config_watcher import ConfigWatcher
from detector import YOLODetector
from evaluator import EvaluationRunner
from events import EventProcessor
from publisher import RedisPublisher
from scheduler import ArmScheduler
from stats_collector import StatsCollector
from stream import RTSPStream

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/scarguard.yml")
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/data/snapshots")
DB_PATH = os.environ.get("DB_PATH", "/data/scarguard.db")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def setup_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,
    )


def _match_action_rules(class_name: str, rules: list[dict]) -> list[str]:
    """Return channel names for the first matching action rule.

    Rules are evaluated in order; the first rule whose class_name matches
    (or is the wildcard "*") wins.  An empty list means "notify all channels".
    """
    for rule in rules:
        rule_class = rule.get("class_name", "*")
        if rule_class == "*" or rule_class == class_name:
            return list(rule.get("channels", []))
    return []


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


def run_camera(
    camera_cfg: dict,
    detector: YOLODetector,
    event_processor: EventProcessor,
    redis_cfg: dict,
    frame_skip_ref: list[int],
    armed_ref: list[bool],
    exclusion_zones_ref: list[list[dict]],
    action_rules_ref: list[list[dict]],
    stop_event: threading.Event,
    camera_stats: dict[str, dict] | None = None,
    camera_stats_lock: threading.Lock | None = None,
) -> None:
    """Per-camera detection loop — runs in its own thread.

    Each thread owns its RTSPStream (with independent reconnect backoff) and
    a dedicated RedisPublisher connection.  Stopping the thread is done by
    setting *stop_event*.
    """
    name = camera_cfg["name"]

    publisher = RedisPublisher(
        host=redis_cfg.get("host", "redis"),
        port=int(redis_cfg.get("port", 6379)),
    )
    stream = RTSPStream(name=name, rtsp_url=camera_cfg["rtsp_url"], stop_event=stop_event)

    logger.info(
        "[%s] Camera thread starting | frame_skip=%d",
        name,
        frame_skip_ref[0],
    )

    frame_count = 0
    # Per-camera inference tracking for stats
    _infer_count = 0
    _infer_total_ms = 0.0
    _infer_window_start = time.monotonic()

    while not stop_event.is_set():
        ret, frame = stream.read()
        if not ret:
            # RTSPStream handles its own backoff; wait briefly so we don't
            # spin-check stop_event too aggressively, but wake up immediately
            # on shutdown rather than blocking for a full second.
            stop_event.wait(1.0)
            continue

        frame_count += 1
        if frame_count % frame_skip_ref[0] != 0:
            continue

        if not armed_ref[0]:
            continue

        t0 = time.monotonic()
        detections = detector.predict(frame)
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
        if detections:
            # Exclusion zones are applied BEFORE cooldown dedup intentionally:
            # an object permanently in an excluded region should not consume the
            # cooldown slot for its class on this camera.
            zones = exclusion_zones_ref[0]  # GIL-safe atomic read of list item
            if zones:
                frame_h, frame_w = frame.shape[:2]
                detections = [
                    det for det in detections
                    if not _in_exclusion_zone(
                        (det.bbox[0] + det.bbox[2]) // 2,
                        (det.bbox[1] + det.bbox[3]) // 2,
                        frame_w, frame_h, zones,
                    )
                ]
                if not detections:
                    logger.debug("[%s] All detections suppressed by exclusion zones", name)
            if detections:
                rules = action_rules_ref[0]  # GIL-safe atomic read
                actions_by_class: dict[str, list[str]] = {}
                if rules:
                    for det in detections:
                        if det.class_name not in actions_by_class:
                            actions_by_class[det.class_name] = _match_action_rules(
                                det.class_name, rules
                            )
                events = event_processor.process(
                    detections, name, frame,
                    actions_by_class=actions_by_class if actions_by_class else None,
                )
                for event in events:
                    publisher.publish(event)

    stream.release()
    logger.info("[%s] Camera thread stopped", name)


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.get("system", {}).get("log_level", "info"))
    logger.info("ScarGuard detector starting")

    # ---- Enabled cameras ------------------------------------------------------
    cameras: list[dict] = [c for c in cfg.get("cameras", []) if c.get("enabled", True)]
    if not cameras:
        logger.error("No enabled cameras found in config — exiting")
        sys.exit(1)

    # ---- Config sections -------------------------------------------------------
    det_cfg: dict = cfg.get("detection", {})
    redis_cfg: dict = cfg.get("redis", {})
    # Mutable references so hot-reload can update these without restarting threads.
    armed_ref: list[bool] = [cfg.get("system", {}).get("armed", True)]
    frame_skip_ref: list[int] = [det_cfg.get("frame_skip", 2)]

    # ---- Shared components -----------------------------------------------------
    detector = YOLODetector(
        model_path=det_cfg["model_path"],
        confidence_threshold=det_cfg.get("confidence_threshold", 0.25),
        target_classes=det_cfg.get("target_classes", []),
    )

    event_processor = EventProcessor(
        cooldown_seconds=det_cfg.get("cooldown_seconds", 30),
        snapshot_dir=SNAPSHOT_DIR,
        db_path=DB_PATH,
    )

    retention_days = int(cfg.get("system", {}).get("snapshot_retention_days", 30))
    cleaner = SnapshotCleaner(
        snapshot_dir=SNAPSHOT_DIR,
        db_path=DB_PATH,
        retention_days=retention_days,
    )
    cleaner.start()

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

        return redis.Redis(
            host=redis_cfg.get("host", "redis"),
            port=int(redis_cfg.get("port", 6379)),
            decode_responses=True,
        )

    scheduler = ArmScheduler(armed_ref, _on_scheduler_transition, get_redis=_make_redis)
    sys_cfg = cfg.get("system", {})
    scheduler.configure(sys_cfg.get("schedule", {}), sys_cfg.get("timezone", "UTC"))
    scheduler.start()

    # ---- Signal handling -------------------------------------------------------
    global_stop = threading.Event()

    def _shutdown(sig: int, _frame: object) -> None:
        logger.info("Received signal %s — shutting down", sig)
        global_stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # ---- Per-camera thread management -----------------------------------------
    # Maps camera name → (thread, per-camera stop event, exclusion_zones_ref, action_rules_ref).
    active_cameras: dict[str, tuple[threading.Thread, threading.Event, list[list[dict]], list[list[dict]]]] = {}
    camera_stats: dict[str, dict] = {}
    camera_stats_lock = threading.Lock()

    def _start_camera(camera_cfg: dict) -> None:
        name = camera_cfg["name"]
        cam_stop = threading.Event()
        zones_ref: list[list[dict]] = [list(camera_cfg.get("exclusion_zones", []))]
        rules_ref: list[list[dict]] = [list(camera_cfg.get("action_rules", []))]
        t = threading.Thread(
            target=run_camera,
            args=(
                camera_cfg,
                detector,
                event_processor,
                redis_cfg,
                frame_skip_ref,
                armed_ref,
                zones_ref,
                rules_ref,
                cam_stop,
                camera_stats,
                camera_stats_lock,
            ),
            name=f"camera-{name}",
            daemon=True,
        )
        t.start()
        active_cameras[name] = (t, cam_stop, zones_ref, rules_ref)
        logger.info("[%s] Camera thread started", name)

    def _stop_camera(name: str) -> None:
        if name not in active_cameras:
            return
        t, cam_stop, _, __ = active_cameras.pop(name)
        cam_stop.set()
        t.join(timeout=5)
        with camera_stats_lock:
            camera_stats.pop(name, None)
        logger.info("[%s] Camera thread stopped", name)

    for camera_cfg in cameras:
        _start_camera(camera_cfg)

    logger.info(
        "Monitoring %d camera(s): %s | armed=%s | cooldown=%ds",
        len(cameras),
        ", ".join(c["name"] for c in cameras),
        armed_ref[0],
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
    )
    stats_collector.start()

    # ---- Model evaluation runner -----------------------------------------------
    eval_runner = EvaluationRunner(
        redis_cfg=redis_cfg,
        db_path=DB_PATH,
        snapshot_dir=SNAPSHOT_DIR,
    )
    eval_runner.start()

    # ---- Config hot-reload ----------------------------------------------------
    def _on_config_change(new_cfg: dict) -> None:
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
        if new_armed != armed_ref[0]:
            armed_ref[0] = new_armed
            changes.append(f"armed={new_armed}")

        # detection params (read by camera threads on each inference call)
        new_conf = new_det.get("confidence_threshold", 0.25)
        if new_conf != detector.confidence_threshold:
            detector.confidence_threshold = new_conf
            changes.append(f"confidence_threshold={new_conf}")

        new_classes = set(new_det.get("target_classes", []))
        if new_classes != detector.target_classes:
            detector.target_classes = new_classes
            changes.append(f"target_classes={sorted(new_classes)}")

        new_cooldown = new_det.get("cooldown_seconds", 30)
        if new_cooldown != event_processor.cooldown_seconds:
            event_processor.cooldown_seconds = new_cooldown
            changes.append(f"cooldown_seconds={new_cooldown}")

        new_frame_skip = new_det.get("frame_skip", 2)
        if new_frame_skip != frame_skip_ref[0]:
            frame_skip_ref[0] = new_frame_skip
            changes.append(f"frame_skip={new_frame_skip}")

        # cameras added / exclusion zones updated
        for cam_cfg in new_cameras_list:
            cam_name = cam_cfg["name"]
            if cam_name not in old_camera_names:
                _start_camera(cam_cfg)
                changes.append(f"camera added: {cam_name}")
            else:
                # Hot-reload exclusion zones and action rules for running cameras.
                # list-item assignment is atomic under CPython's GIL (same pattern
                # as armed_ref and frame_skip_ref).
                _, _, zones_ref, rules_ref = active_cameras[cam_name]
                new_zones = list(cam_cfg.get("exclusion_zones", []))
                if new_zones != zones_ref[0]:
                    zones_ref[0] = new_zones
                    changes.append(f"exclusion_zones updated: {cam_name}")
                new_rules = list(cam_cfg.get("action_rules", []))
                if new_rules != rules_ref[0]:
                    rules_ref[0] = new_rules
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
    event_processor.close()
    cleaner.stop()

    logger.info("Detector stopped cleanly")


if __name__ == "__main__":
    main()
