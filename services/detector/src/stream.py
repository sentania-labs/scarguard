"""RTSP stream reader with automatic reconnect and exponential backoff."""

import logging
import os
import threading

import cv2
import numpy as np

# 5-second RTSP socket I/O timeout — prevents cap.read() from blocking
# indefinitely when a stream hangs after connect (server stops sending data).
# Uses setdefault so a value already set in the environment is preserved.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000")

logger = logging.getLogger(__name__)


class RTSPStream:
    def __init__(
        self,
        name: str,
        rtsp_url: str,
        reconnect_delay: float = 5.0,
        max_reconnect_delay: float = 60.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.name = name
        self.rtsp_url = rtsp_url
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._cap: cv2.VideoCapture | None = None
        self._current_delay = reconnect_delay
        self._stop_event = stop_event or threading.Event()

    def _open(self) -> bool:
        if self._cap is not None:
            self._cap.release()

        self._cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
        # Keep buffer at 1 frame so we always process the latest image,
        # not a stale queued frame.
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if self._cap.isOpened():
            self._current_delay = self._reconnect_delay
            logger.info("[%s] Connected to RTSP stream", self.name)
            return True

        logger.warning("[%s] Failed to open RTSP stream: %s", self.name, self.rtsp_url)
        return False

    def _reconnect(self) -> bool:
        logger.info(
            "[%s] Reconnecting in %.1fs (next backoff: %.1fs)",
            self.name,
            self._current_delay,
            min(self._current_delay * 2, self._max_reconnect_delay),
        )
        # wait() returns True immediately if stop_event is already set,
        # or when it becomes set during the wait — either way we abort.
        if self._stop_event.wait(timeout=self._current_delay):
            return False
        success = self._open()
        if not success:
            self._current_delay = min(self._current_delay * 2, self._max_reconnect_delay)
        return success

    def read(self) -> tuple[bool, np.ndarray | None]:
        """Return (True, frame) on success or (False, None) when unavailable."""
        if self._stop_event.is_set():
            return False, None
        if self._cap is None or not self._cap.isOpened():
            if not self._reconnect():
                return False, None

        ret, frame = self._cap.read()
        if not ret:
            logger.warning("[%s] Read failed — stream dropped", self.name)
            self._cap.release()
            self._cap = None
            return False, None

        return True, frame

    def release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        logger.info("[%s] Stream released", self.name)
