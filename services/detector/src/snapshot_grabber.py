"""On-demand camera snapshot grabber — listens for Redis requests."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import redis as redis_lib

logger = logging.getLogger(__name__)

SNAPSHOT_DIR = Path(os.environ.get("SNAPSHOT_DIR", "/data/snapshots"))
REQUEST_CHANNEL = "scarguard:snapshot:request"


class SnapshotGrabber(threading.Thread):
    """Daemon thread that listens for snapshot requests and grabs frames."""

    def __init__(
        self,
        redis_cfg: dict,
        cameras_cfg: list[dict],
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="snapshot-grabber", daemon=True)
        self._redis_cfg = redis_cfg
        self._cameras = {c["name"]: c for c in cameras_cfg if c.get("enabled", True)}
        self._stop = stop_event
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="snap")

    def update_cameras(self, cameras_cfg: list[dict]) -> None:
        """Update camera config (called on hot-reload)."""
        with self._lock:
            self._cameras = {c["name"]: c for c in cameras_cfg if c.get("enabled", True)}

    def run(self) -> None:
        logger.info("SnapshotGrabber started")

        while not self._stop.is_set():
            client: redis_lib.Redis | None = None  # type: ignore[type-arg]
            pubsub = None
            try:
                _pw = os.environ.get("REDIS_PASSWORD", "") or None
                client = redis_lib.Redis(
                    host=self._redis_cfg.get("host", "redis"),
                    port=int(self._redis_cfg.get("port", 6379)),
                    password=_pw,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    retry_on_timeout=True,
                )
                pubsub = client.pubsub()
                pubsub.subscribe(REQUEST_CHANNEL)

                while not self._stop.is_set():
                    msg = pubsub.get_message(timeout=1.0)
                    if msg is None or msg["type"] != "message":
                        continue

                    try:
                        request = json.loads(msg["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue

                    camera_name = request.get("camera_name", "")
                    request_id = request.get("request_id", "")
                    if not camera_name or not request_id:
                        continue

                    self._pool.submit(self._handle_request, client, camera_name, request_id)

            except redis_lib.RedisError:
                logger.warning("SnapshotGrabber Redis error — reconnecting", exc_info=True)
            except Exception:
                logger.exception("SnapshotGrabber error — reconnecting")
            finally:
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:
                        pass

            if not self._stop.is_set():
                self._stop.wait(2)

        self._pool.shutdown(wait=False)
        logger.info("SnapshotGrabber stopped")

    def _handle_request(
        self, client: redis_lib.Redis, camera_name: str, request_id: str  # type: ignore[type-arg]
    ) -> None:
        """Grab a single frame from the camera and publish the result."""
        result_channel = f"scarguard:snapshot:result:{request_id}"

        with self._lock:
            cam_cfg = self._cameras.get(camera_name)

        if cam_cfg is None:
            client.publish(result_channel, json.dumps({
                "ok": False, "error": f"Camera '{camera_name}' not found or disabled",
            }))
            return

        rtsp_url = cam_cfg.get("rtsp_url", "")
        if not rtsp_url:
            client.publish(result_channel, json.dumps({
                "ok": False, "error": f"No RTSP URL for camera '{camera_name}'",
            }))
            return

        try:
            import cv2

            cap = cv2.VideoCapture(rtsp_url)
            try:
                cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
                cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)

                if not cap.isOpened():
                    client.publish(result_channel, json.dumps({
                        "ok": False, "error": f"Cannot open stream for '{camera_name}'",
                    }))
                    return

                ret, frame = cap.read()
            finally:
                cap.release()

            if not ret or frame is None:
                client.publish(result_channel, json.dumps({
                    "ok": False, "error": f"Failed to read frame from '{camera_name}'",
                }))
                return

            # Sanitize camera name for filesystem safety
            safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", camera_name)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            filename = f"{safe_name}_snapshot_{ts}.jpg"
            filepath = SNAPSHOT_DIR / filename
            cv2.imwrite(str(filepath), frame)

            client.publish(result_channel, json.dumps({
                "ok": True, "snapshot_path": str(filepath), "filename": filename,
            }))
            logger.info("Snapshot grabbed: %s -> %s", camera_name, filename)

        except Exception as exc:
            client.publish(result_channel, json.dumps({
                "ok": False, "error": str(exc),
            }))
            logger.exception("Snapshot grab failed for %s", camera_name)
