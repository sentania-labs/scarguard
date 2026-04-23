"""RTSP stream reader with automatic reconnect and exponential backoff."""

from __future__ import annotations

import logging
import os
import threading

import cv2
import numpy as np


def _ensure_ffmpeg_socket_timeout(
    options: str | None,
    timeout_us: int = 5_000_000,
    default_options: str = "rtsp_transport;tcp",
) -> str:
    """Return capture options with defaults and stimeout ensured."""
    source_options = options if options and options.strip() else default_options

    merged: list[str] = []
    has_stimeout = False
    for token in source_options.split("|"):
        token = token.strip()
        if not token:
            continue
        key, _sep, _value = token.partition(";")
        if key == "stimeout":
            if not has_stimeout:
                merged.append(f"stimeout;{timeout_us}")
                has_stimeout = True
            continue
        merged.append(token)

    if not has_stimeout:
        merged.append(f"stimeout;{timeout_us}")

    return "|".join(merged)


# Ensure a 5-second RTSP socket I/O timeout so cap.read() won't block
# indefinitely when a stream hangs after connect.
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = _ensure_ffmpeg_socket_timeout(
    os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
)

logger = logging.getLogger(__name__)


class RTSPStream:
    # v1.14: after this many consecutive reconnect attempts, jump the
    # backoff cadence to ``LONG_BACKOFF_SEC``. Catches the "camera is
    # permanently gone" case (operator removed it from the network) so
    # we don't fill the logs with one warning every 60 seconds forever.
    LONG_BACKOFF_AFTER_FAILURES = 20
    LONG_BACKOFF_SEC = 3600.0

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
        self._consecutive_failures = 0
        self._stop_event = stop_event or threading.Event()

    def _open(self) -> bool:
        if self._cap is not None:
            self._cap.release()

        self._cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
        # Keep buffer at 1 frame so we always process the latest image,
        # not a stale queued frame.
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if self._cap.isOpened():
            # Reset both the backoff window and the long-backoff counter so
            # a previously-flaky stream that recovers gets quick reconnects
            # again.
            self._current_delay = self._reconnect_delay
            self._consecutive_failures = 0
            logger.info("[%s] Connected to RTSP stream", self.name)
            return True

        logger.warning("[%s] Failed to open RTSP stream: %s", self.name, self.rtsp_url)
        return False

    def _reconnect(self) -> bool:
        # If we've crossed the long-backoff threshold, hold at the long
        # cadence instead of the short exponential window. Avoids a noisy
        # log line every minute when a camera is permanently offline.
        if self._consecutive_failures >= self.LONG_BACKOFF_AFTER_FAILURES:
            wait_sec = self.LONG_BACKOFF_SEC
            logger.warning(
                "[%s] Stream offline for %d attempts — backing off to %.0f min between retries",
                self.name, self._consecutive_failures, wait_sec / 60.0,
            )
        else:
            wait_sec = self._current_delay
            logger.info(
                "[%s] Reconnecting in %.1fs (next backoff: %.1fs)",
                self.name,
                wait_sec,
                min(self._current_delay * 2, self._max_reconnect_delay),
            )
        # wait() returns True immediately if stop_event is already set,
        # or when it becomes set during the wait — either way we abort.
        if self._stop_event.wait(timeout=wait_sec):
            return False
        success = self._open()
        if not success:
            self._consecutive_failures += 1
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

    def grab(self) -> bool:
        """Advance the stream by one frame without decoding.

        Returns True on success.  On failure, releases the capture so the
        next call to :meth:`read` or :meth:`grab` triggers a reconnect.
        """
        if self._stop_event.is_set():
            return False
        if self._cap is None or not self._cap.isOpened():
            if not self._reconnect():
                return False

        ret = self._cap.grab()
        if not ret:
            logger.warning("[%s] Grab failed — stream dropped", self.name)
            self._cap.release()
            self._cap = None
            return False

        return True

    def release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        logger.info("[%s] Stream released", self.name)
