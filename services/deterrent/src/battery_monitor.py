"""Periodic battery monitoring for Tuya devices.

Polls device status via the Cloud API on a configurable interval and
publishes low-battery alerts to the ``scarguard:notifications`` Redis channel.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

import redis as redis_lib
from actuation_models import ActuationConfig, DeviceConfig
from cloud_controller import TuyaCloudController

logger = logging.getLogger(__name__)


class BatteryMonitor:
    """Background daemon thread that polls battery levels."""

    def __init__(
        self,
        controller: TuyaCloudController,
        redis_client: redis_lib.Redis,
    ) -> None:
        self._controller = controller
        self._redis = redis_client
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._config: ActuationConfig | None = None

    def configure(self, config: ActuationConfig) -> None:
        """Update configuration (called on hot-reload)."""
        self._config = config

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._shutdown.clear()
        self._thread = threading.Thread(
            target=self._run, name="battery-monitor", daemon=True,
        )
        self._thread.start()
        logger.info("Battery monitor started")

    def stop(self) -> None:
        self._shutdown.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        logger.info("Battery monitor stopped")

    def _run(self) -> None:
        while not self._shutdown.is_set():
            cfg = self._config
            if cfg is None or not cfg.battery_monitor.enabled:
                self._shutdown.wait(60)
                continue

            interval_sec = cfg.battery_monitor.check_interval_hours * 3600
            self._check_all(cfg)
            self._shutdown.wait(interval_sec)

    def _check_all(self, cfg: ActuationConfig) -> None:
        """Poll battery for every enabled device and alert if low."""
        threshold = cfg.battery_monitor.alert_threshold_percent
        for device in cfg.devices:
            if not device.enabled:
                continue
            self._check_device(device, threshold)

    def _check_device(self, device: DeviceConfig, threshold: int) -> None:
        status = self._controller.get_device_status(device.device_id)
        if status is None:
            logger.warning("Battery check skipped for %s — status unavailable", device.name)
            return

        battery = status.get("battery_percentage")
        if battery is None:
            # Device may not have a battery DP (e.g. smart plugs)
            return

        logger.info("Device %s battery: %d%%", device.name, battery)
        if battery <= threshold:
            self._publish_alert(device, battery, threshold)

    def _publish_alert(self, device: DeviceConfig, battery: int, threshold: int) -> None:
        alert = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "class_name": "low_battery",
            "confidence": 1.0,
            "camera_name": f"deterrent:{device.name}",
            "snapshot_path": None,
            "message": (
                f"Deterrent device \"{device.name}\" battery is at {battery}% "
                f"(threshold: {threshold}%)"
            ),
        }
        try:
            self._redis.publish("scarguard:detections", json.dumps(alert))
            logger.warning(
                "Low battery alert: %s at %d%% (threshold %d%%)",
                device.name, battery, threshold,
            )
        except Exception:
            logger.exception("Failed to publish battery alert for %s", device.name)
