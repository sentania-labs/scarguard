"""ScarGuard detector — main loop.

Reads frames from one or more RTSP streams concurrently, runs YOLO inference,
applies cooldown deduplication, persists events to SQLite, and publishes to Redis.

Each enabled camera runs in its own thread.  The YOLODetector and EventProcessor
are shared across threads; both are internally thread-safe.  Each camera thread
owns its own RTSPStream and RedisPublisher connection.
"""

import logging
import os
import signal
import sys
import threading
import yaml
from config_watcher import ConfigWatcher
from detector import YOLODetector
from events import EventProcessor
from publisher import RedisPublisher
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


def run_camera(
    camera_cfg: dict,
    detector: YOLODetector,
    event_processor: EventProcessor,
    redis_cfg: dict,
    frame_skip_ref: list[int],
    armed_ref: list[bool],
    stop_event: threading.Event,
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

        detections = detector.predict(frame)
        if detections:
            events = event_processor.process(detections, name, frame)
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

    # ---- Signal handling -------------------------------------------------------
    global_stop = threading.Event()

    def _shutdown(sig: int, _frame: object) -> None:
        logger.info("Received signal %s — shutting down", sig)
        global_stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # ---- Per-camera thread management -----------------------------------------
    # Maps camera name → (thread, per-camera stop event).
    active_cameras: dict[str, tuple[threading.Thread, threading.Event]] = {}

    def _start_camera(camera_cfg: dict) -> None:
        name = camera_cfg["name"]
        cam_stop = threading.Event()
        t = threading.Thread(
            target=run_camera,
            args=(
                camera_cfg,
                detector,
                event_processor,
                redis_cfg,
                frame_skip_ref,
                armed_ref,
                cam_stop,
            ),
            name=f"camera-{name}",
            daemon=True,
        )
        t.start()
        active_cameras[name] = (t, cam_stop)
        logger.info("[%s] Camera thread started", name)

    def _stop_camera(name: str) -> None:
        if name not in active_cameras:
            return
        t, cam_stop = active_cameras.pop(name)
        cam_stop.set()
        t.join(timeout=5)
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

        # cameras added
        for cam_cfg in new_cameras_list:
            if cam_cfg["name"] not in old_camera_names:
                _start_camera(cam_cfg)
                changes.append(f"camera added: {cam_cfg['name']}")

        # cameras removed or disabled
        for name in old_camera_names - new_camera_names:
            _stop_camera(name)
            changes.append(f"camera removed: {name}")

        if changes:
            logger.info("Config reloaded — changes: %s", ", ".join(changes))
        else:
            logger.info("Config reloaded — no effective changes")

    watcher = ConfigWatcher(CONFIG_PATH, _on_config_change)
    watcher.start()

    # ---- Wait for shutdown -----------------------------------------------------
    global_stop.wait()

    watcher.stop()
    for name in list(active_cameras.keys()):
        _stop_camera(name)

    logger.info("Detector stopped cleanly")


if __name__ == "__main__":
    main()
