"""Per-camera health tracking with offline alerts."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class _CameraState:
    """Internal state for a single camera."""

    last_frame_at: float = 0.0  # monotonic timestamp of last successful frame
    last_failure_at: float = 0.0  # monotonic timestamp of last failure
    offline_since: float | None = None  # monotonic timestamp when camera went offline
    reconnect_count: int = 0
    last_reconnect_start: float = 0.0
    last_reconnect_duration: float | None = None
    alert_sent: bool = False  # True if offline alert already dispatched for this outage


class CameraHealthTracker:
    """Tracks per-camera health and generates alerts for prolonged outages.

    Thread-safe — called from multiple camera threads and the stats collector.
    """

    def __init__(
        self,
        alert_threshold_seconds: int = 600,
        debounce_seconds: int = 30,
    ) -> None:
        self._alert_threshold = alert_threshold_seconds
        self._debounce = debounce_seconds
        self._cameras: dict[str, _CameraState] = {}
        self._lock = threading.Lock()

    def record_frame(self, camera_name: str) -> None:
        """Record a successful frame read — camera is online."""
        now = time.monotonic()
        with self._lock:
            state = self._cameras.setdefault(camera_name, _CameraState())
            state.last_frame_at = now
            if state.offline_since is not None:
                # Camera came back online
                state.last_reconnect_duration = now - state.offline_since
                state.reconnect_count += 1
                logger.info(
                    "[%s] Camera back online (was offline %.1fs, reconnect #%d)",
                    camera_name,
                    state.last_reconnect_duration,
                    state.reconnect_count,
                )
                state.offline_since = None
                state.alert_sent = False

    def record_failure(self, camera_name: str) -> None:
        """Record a failed frame read — camera may be going offline."""
        now = time.monotonic()
        with self._lock:
            state = self._cameras.setdefault(camera_name, _CameraState())
            state.last_failure_at = now
            if state.offline_since is None:
                # Only mark offline after debounce period with no frames
                if state.last_frame_at == 0 or (now - state.last_frame_at) > self._debounce:
                    state.offline_since = now
                    state.last_reconnect_start = now

    def check_alerts(self) -> list[dict]:
        """Return alert events for cameras that have been offline beyond threshold.

        Each camera only generates one alert per outage (until it recovers).
        """
        now = time.monotonic()
        alerts: list[dict] = []
        with self._lock:
            for name, state in self._cameras.items():
                if (
                    state.offline_since is not None
                    and not state.alert_sent
                    and (now - state.offline_since) >= self._alert_threshold
                ):
                    state.alert_sent = True
                    offline_secs = now - state.offline_since
                    alerts.append({
                        "type": "camera_offline",
                        "camera_name": name,
                        "offline_seconds": round(offline_secs, 1),
                        "reconnect_count": state.reconnect_count,
                    })
                    logger.warning(
                        "[%s] Camera offline alert — down for %.0fs",
                        name,
                        offline_secs,
                    )
        return alerts

    def get_all_status(self) -> dict[str, dict]:
        """Return health status for all tracked cameras.

        Returns dict keyed by camera name with:
        - state: "online" | "degraded" | "offline"
        - offline_seconds: float | None
        - reconnect_count: int
        - last_reconnect_duration: float | None
        """
        now = time.monotonic()
        result: dict[str, dict] = {}
        with self._lock:
            for name, state in self._cameras.items():
                if state.offline_since is not None:
                    offline_secs = now - state.offline_since
                    cam_state = "offline" if offline_secs > self._debounce else "degraded"
                elif state.last_frame_at == 0:
                    cam_state = "offline"
                    offline_secs = None
                else:
                    # Online, but check staleness (no frame in 2x debounce = degraded)
                    since_last = now - state.last_frame_at
                    if since_last > self._debounce * 2:
                        cam_state = "degraded"
                    else:
                        cam_state = "online"
                    offline_secs = None

                result[name] = {
                    "state": cam_state,
                    "offline_seconds": (
                        round(offline_secs, 1) if offline_secs is not None else None
                    ),
                    "reconnect_count": state.reconnect_count,
                    "last_reconnect_duration": (
                        round(state.last_reconnect_duration, 1)
                        if state.last_reconnect_duration is not None
                        else None
                    ),
                }
        return result

    def remove_camera(self, camera_name: str) -> None:
        """Stop tracking a camera (e.g. when disabled via hot-reload)."""
        with self._lock:
            self._cameras.pop(camera_name, None)
