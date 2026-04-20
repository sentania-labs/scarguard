"""Tuya Cloud API wrapper for device control."""

from __future__ import annotations

import logging
import time
from typing import Any

import tinytuya
from actuation_models import DeviceConfig

logger = logging.getLogger(__name__)

# Default DP codes for on/off by device type.  Can be overridden per-device
# via the ``dp_code`` config field.
_DEFAULT_DP_CODES: dict[str, str] = {
    "sprinkler": "switch_1",
    "light": "switch_led",
    "sound": "switch",
    "plug": "switch_1",
}


class TuyaCloudController:
    """Controls Tuya devices via the Cloud API.

    Uses ``tinytuya.Cloud`` which makes signed HTTPS REST calls to Tuya's
    OpenAPI.  Token refresh is handled automatically by the library.
    """

    def __init__(self, api_key: str, api_secret: str, api_region: str = "us") -> None:
        self._cloud = tinytuya.Cloud(
            apiRegion=api_region,
            apiKey=api_key,
            apiSecret=api_secret,
        )
        logger.info("Tuya Cloud controller initialised (region=%s)", api_region)

    def _dp_code_for(self, device: DeviceConfig) -> str:
        """Return the DP code to use for on/off toggling."""
        return device.dp_code or _DEFAULT_DP_CODES.get(device.type, "switch_1")

    def activate_device(
        self, device: DeviceConfig, duration_sec: float,
    ) -> tuple[bool, str | None, float | None]:
        """Turn *device* ON, wait *duration_sec*, then turn it OFF.

        Returns ``(success, error_message, cloud_ack_ms)``.  A partial
        success (ON worked but OFF failed) still returns ``True`` — the
        device will auto-timeout on most Tuya firmware.  ``cloud_ack_ms``
        is the elapsed time between sending the ON command to Tuya Cloud
        and receiving a success response — useful for diagnosing battery-
        device deep-sleep latency.  ``None`` if the ON call raised.
        """
        dp_code = self._dp_code_for(device)
        error: str | None = None

        # --- ON ---
        t_on = time.monotonic()
        try:
            on_cmd: dict[str, Any] = {"commands": [{"code": dp_code, "value": True}]}
            result = self._cloud.sendcommand(device.device_id, on_cmd)
            on_ack_ms = (time.monotonic() - t_on) * 1000.0
            if not result.get("success"):
                msg = f"ON failed: {result}"
                logger.error("Device %s (%s) — %s", device.name, device.device_id, msg)
                return False, msg, on_ack_ms
            logger.info(
                "Device %s ON (dp=%s) cloud_ack=%.0fms",
                device.name, dp_code, on_ack_ms,
            )
        except Exception as exc:
            msg = f"ON exception: {exc}"
            logger.error("Device %s (%s) — %s", device.name, device.device_id, msg)
            return False, msg, None

        # --- HOLD ---
        time.sleep(duration_sec)

        # --- OFF ---
        try:
            off_cmd: dict[str, Any] = {"commands": [{"code": dp_code, "value": False}]}
            result = self._cloud.sendcommand(device.device_id, off_cmd)
            if not result.get("success"):
                error = f"OFF failed (device may auto-timeout): {result}"
                logger.warning("Device %s (%s) — %s", device.name, device.device_id, error)
            else:
                logger.info("Device %s OFF", device.name)
        except Exception as exc:
            error = f"OFF exception (device may auto-timeout): {exc}"
            logger.warning("Device %s (%s) — %s", device.name, device.device_id, error)

        return True, error, on_ack_ms

    def get_device_status(self, device_id: str) -> dict[str, Any] | None:
        """Query device status (battery level, switch state, etc.).

        Returns the parsed status dict on success, or ``None`` on failure.
        """
        try:
            result = self._cloud.getstatus(device_id)
            if result.get("success") and result.get("result"):
                return {item["code"]: item["value"] for item in result["result"]}
            logger.warning("Status query failed for %s: %s", device_id, result)
            return None
        except Exception:
            logger.exception("Status query exception for %s", device_id)
            return None
