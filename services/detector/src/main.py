"""ScarGuard detector — main loop.

Reads frames from an RTSP stream, runs YOLO inference, applies cooldown
deduplication, persists events to SQLite, and publishes to Redis.
"""

import logging
import os
import signal
import sys
import time

import yaml
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


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.get("system", {}).get("log_level", "info"))
    logger.info("ScarGuard detector starting")

    # ---- Camera -------------------------------------------------------
    cameras: list[dict] = cfg.get("cameras", [])
    camera_cfg = next((c for c in cameras if c.get("enabled", True)), None)
    if camera_cfg is None:
        logger.error("No enabled cameras found in config — exiting")
        sys.exit(1)

    # ---- Config sections ----------------------------------------------
    det_cfg: dict = cfg.get("detection", {})
    redis_cfg: dict = cfg.get("redis", {})
    armed: bool = cfg.get("system", {}).get("armed", True)
    frame_skip: int = det_cfg.get("frame_skip", 2)

    # ---- Components ---------------------------------------------------
    stream = RTSPStream(name=camera_cfg["name"], rtsp_url=camera_cfg["rtsp_url"])

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

    publisher = RedisPublisher(
        host=redis_cfg.get("host", "redis"),
        port=int(redis_cfg.get("port", 6379)),
    )

    # ---- Signal handling ----------------------------------------------
    running = True

    def _shutdown(sig: int, _frame: object) -> None:
        nonlocal running
        logger.info("Received signal %s — shutting down", sig)
        running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # ---- Main loop ----------------------------------------------------
    logger.info(
        "Monitoring camera '%s' | armed=%s | frame_skip=%d | cooldown=%ds",
        camera_cfg["name"],
        armed,
        frame_skip,
        det_cfg.get("cooldown_seconds", 30),
    )

    frame_count = 0
    while running:
        ret, frame = stream.read()
        if not ret:
            # Stream unavailable; back off briefly before retrying
            time.sleep(1)
            continue

        frame_count += 1
        if frame_count % frame_skip != 0:
            continue

        if not armed:
            continue

        detections = detector.predict(frame)
        if detections:
            events = event_processor.process(detections, camera_cfg["name"], frame)
            for event in events:
                publisher.publish(event)

    stream.release()
    logger.info("Detector stopped cleanly")


if __name__ == "__main__":
    main()
