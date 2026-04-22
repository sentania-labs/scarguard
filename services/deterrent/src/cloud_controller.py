"""Tuya Cloud API wrapper for device control."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import tinytuya
from actuation_models import DeviceConfig
from deterrent_safety import (
    DEFAULT_TEST_FIRE_SEC,
    MAX_ACTUATION_SEC,
    OFF_RETRY_BACKOFF_SEC,
    clamp_duration,
)

logger = logging.getLogger(__name__)

# Default DP codes for on/off by device type.  Can be overridden per-device
# via the ``dp_code`` config field.
_DEFAULT_DP_CODES: dict[str, str] = {
    "sprinkler": "switch_1",
    "light": "switch_led",
    "sound": "switch",
    "plug": "switch_1",
}


@dataclass
class ActivationResult:
    """Outcome of a single ``activate_device`` call.

    Attributes
    ----------
    on_success:
        True iff the ON command was acknowledged by Tuya Cloud.
    off_success:
        True iff the OFF command was ultimately acknowledged (after
        retries). ``None`` if ON failed and OFF was never attempted.
    stuck:
        True iff ON succeeded but OFF ultimately failed. The device
        may be physically still-on; caller should publish a ``deterrent:stuck``
        event and surface to the operator.
    error:
        Human-readable description of the failure, if any.
    on_ack_ms:
        Cloud ack latency for the ON command, in ms. ``None`` if ON raised.
    off_attempts:
        Number of OFF attempts made (1 = single success, >1 = retries).
        0 if ON failed and OFF was never attempted.
    """

    on_success: bool
    off_success: bool | None
    error: str | None
    on_ack_ms: float | None
    off_attempts: int

    @property
    def success(self) -> bool:
        """Fully successful iff both ON and OFF succeeded."""
        return self.on_success and self.off_success is True

    @property
    def stuck(self) -> bool:
        """ON succeeded but OFF didn't — device may be physically stuck on."""
        return self.on_success and self.off_success is False


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
        self._lock = threading.Lock()
        # Busy tracking — device_id → True while activate_device is running for
        # that device. Consulted by the reconciliation loop so it doesn't
        # race a legitimate in-flight actuation.
        self._busy_lock = threading.Lock()
        self._busy: set[str] = set()
        logger.info("Tuya Cloud controller initialised (region=%s)", api_region)

    def dp_code_for(self, device: DeviceConfig) -> str:
        """Return the DP code to use for on/off toggling."""
        return device.dp_code or _DEFAULT_DP_CODES.get(device.type, "switch_1")

    # Back-compat alias — older callers may still use the underscore name.
    _dp_code_for = dp_code_for

    def is_device_busy(self, device_id: str) -> bool:
        """True iff an :meth:`activate_device` call is currently in flight
        for *device_id*. Used by the reconciliation loop."""
        with self._busy_lock:
            return device_id in self._busy

    def is_switched_on(self, device: DeviceConfig) -> bool | None:
        """Query device cloud status and return True/False for switch state,
        or ``None`` if the device is unreachable or reports no switch DP."""
        status = self.get_device_status(device.device_id)
        if status is None:
            return None
        dp = self.dp_code_for(device)
        value = status.get(dp)
        if value is None:
            # Fallback: many Tuya SKUs report switch state under a handful of
            # aliases — try the common ones before giving up.
            for alias in ("switch_1", "switch", "switch_led"):
                if alias in status:
                    value = status[alias]
                    break
        if isinstance(value, bool):
            return value
        return None

    def activate_device(
        self,
        device: DeviceConfig,
        duration_sec: float,
        *,
        request_id: str | None = None,
        event_type: str = "detection",
    ) -> ActivationResult:
        """Turn *device* ON, wait *duration_sec*, then turn it OFF.

        Physical-safety contract:

        * ``duration_sec`` is defence-in-depth clamped to
          ``[MIN_ACTUATION_SEC, MAX_ACTUATION_SEC]``. The web layer
          validates and rejects out-of-range; the clamp here is the final
          backstop.
        * A watchdog timer fires unconditional OFF if the total elapsed
          time from the ON command exceeds ``MAX_ACTUATION_SEC``, regardless
          of whether this function's own OFF call completed. Covers the
          case where the sequence hangs or the thread is killed.
        * OFF is retried with exponential backoff. If all retries fail, the
          returned :class:`ActivationResult` has ``stuck=True``; the caller
          is expected to publish a ``scarguard:deterrent:stuck`` event and
          the reconciliation loop will keep trying.
        """
        dp_code = self._dp_code_for(device)

        duration_sec = clamp_duration(
            duration_sec,
            max_sec=MAX_ACTUATION_SEC,
            default=DEFAULT_TEST_FIRE_SEC,
        )

        with self._busy_lock:
            self._busy.add(device.device_id)

        try:
            return self._activate_device_inner(
                device, duration_sec, dp_code,
                request_id=request_id, event_type=event_type,
            )
        finally:
            with self._busy_lock:
                self._busy.discard(device.device_id)

    def _activate_device_inner(
        self,
        device: DeviceConfig,
        duration_sec: float,
        dp_code: str,
        *,
        request_id: str | None,
        event_type: str,
    ) -> ActivationResult:
        # --- ON ---
        t_on = time.monotonic()
        try:
            on_cmd: dict[str, Any] = {"commands": [{"code": dp_code, "value": True}]}
            with self._lock:
                result = self._cloud.sendcommand(device.device_id, on_cmd)
            on_ack_ms = (time.monotonic() - t_on) * 1000.0
            if not result.get("success"):
                msg = f"ON failed: {result}"
                logger.error(
                    "Device %s (%s) — %s [rid=%s type=%s]",
                    device.name, device.device_id, msg, request_id, event_type,
                )
                return ActivationResult(
                    on_success=False, off_success=None, error=msg,
                    on_ack_ms=on_ack_ms, off_attempts=0,
                )
            logger.info(
                "Device %s ON (dp=%s) cloud_ack=%.0fms [rid=%s type=%s]",
                device.name, dp_code, on_ack_ms, request_id, event_type,
            )
        except Exception as exc:
            msg = f"ON exception: {exc}"
            logger.error(
                "Device %s (%s) — %s [rid=%s type=%s]",
                device.name, device.device_id, msg, request_id, event_type,
            )
            return ActivationResult(
                on_success=False, off_success=None, error=msg,
                on_ack_ms=None, off_attempts=0,
            )

        # Watchdog — unconditional OFF at MAX_ACTUATION_SEC from ON send.
        # Belt-and-suspenders: if the OFF path below succeeds cleanly we
        # cancel this; if the thread dies, hangs, or the OFF retry budget
        # is exhausted without success, the watchdog still forces OFF.
        watchdog = threading.Timer(
            MAX_ACTUATION_SEC,
            self._watchdog_fire,
            args=(device, dp_code, request_id),
        )
        watchdog.daemon = True
        watchdog.start()

        try:
            time.sleep(duration_sec)
            off_success, off_error, off_attempts = self._send_off_with_retry(
                device, dp_code, request_id=request_id,
            )
        finally:
            watchdog.cancel()

        if off_success:
            return ActivationResult(
                on_success=True, off_success=True, error=None,
                on_ack_ms=on_ack_ms, off_attempts=off_attempts,
            )

        return ActivationResult(
            on_success=True,
            off_success=False,
            error=off_error,
            on_ack_ms=on_ack_ms,
            off_attempts=off_attempts,
        )

    def force_off(
        self,
        device: DeviceConfig,
        *,
        request_id: str | None = None,
    ) -> tuple[bool, str | None]:
        """Send OFF to *device* with retries — used by the reconciliation loop
        and the admin emergency-off endpoint.

        Returns ``(ok, error_message)``. Unlike :meth:`activate_device`,
        this does not start a watchdog — the caller invokes it precisely
        to recover from a stuck state.
        """
        dp_code = self._dp_code_for(device)
        ok, err, _ = self._send_off_with_retry(
            device, dp_code, request_id=request_id,
        )
        return ok, err

    def _send_off_with_retry(
        self,
        device: DeviceConfig,
        dp_code: str,
        *,
        request_id: str | None = None,
    ) -> tuple[bool, str | None, int]:
        """Send OFF with exponential backoff. Returns (ok, err, attempts)."""
        off_cmd: dict[str, Any] = {"commands": [{"code": dp_code, "value": False}]}
        last_error: str | None = None
        attempts = 0
        total_attempts = 1 + len(OFF_RETRY_BACKOFF_SEC)

        for attempt_idx in range(total_attempts):
            if attempt_idx > 0:
                time.sleep(OFF_RETRY_BACKOFF_SEC[attempt_idx - 1])
            attempts += 1
            try:
                with self._lock:
                    result = self._cloud.sendcommand(device.device_id, off_cmd)
                if result.get("success"):
                    if attempt_idx == 0:
                        logger.info(
                            "Device %s OFF [rid=%s]", device.name, request_id,
                        )
                    else:
                        logger.warning(
                            "Device %s OFF succeeded on retry %d [rid=%s]",
                            device.name, attempts, request_id,
                        )
                    return True, None, attempts
                last_error = f"OFF returned non-success: {result}"
            except Exception as exc:
                last_error = f"OFF exception: {exc}"
            logger.error(
                "Device %s OFF attempt %d/%d failed — %s [rid=%s]",
                device.name, attempts, total_attempts, last_error, request_id,
            )

        return False, f"OFF_FAILED:{device.device_id}: {last_error}", attempts

    def _watchdog_fire(
        self,
        device: DeviceConfig,
        dp_code: str,
        request_id: str | None,
    ) -> None:
        """Fires MAX_ACTUATION_SEC after ON send if the normal OFF path hasn't
        cancelled the timer. Unconditional force-OFF backstop."""
        logger.critical(
            "WATCHDOG — device %s (%s) exceeded %.1fs — forcing OFF [rid=%s]",
            device.name, device.device_id, MAX_ACTUATION_SEC, request_id,
        )
        try:
            self._send_off_with_retry(device, dp_code, request_id=request_id)
        except Exception:
            logger.exception(
                "Watchdog force-OFF raised for %s (%s) [rid=%s]",
                device.name, device.device_id, request_id,
            )

    def get_device_status(self, device_id: str) -> dict[str, Any] | None:
        """Query device status (battery level, switch state, etc.).

        Returns the parsed status dict on success, or ``None`` on failure.
        """
        try:
            with self._lock:
                result = self._cloud.getstatus(device_id)
            if result.get("success") and result.get("result"):
                return {item["code"]: item["value"] for item in result["result"]}
            logger.warning("Status query failed for %s: %s", device_id, result)
            return None
        except Exception:
            logger.exception("Status query exception for %s", device_id)
            return None
