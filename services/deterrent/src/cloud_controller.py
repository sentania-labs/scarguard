"""Tuya Cloud API wrapper for device control."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import tinytuya
from activation_lease import ActivationLease, RedisActivationLeases
from actuation_models import DeviceConfig
from deterrent_safety import (
    CLOUD_CALL_TIMEOUT_SEC,
    DEFAULT_TEST_FIRE_SEC,
    MAX_ACTUATION_SEC,
    OFF_RETRY_BACKOFF_SEC,
    clamp_duration,
)

logger = logging.getLogger(__name__)

# Credential hot reload replaces the controller while old work can still hold
# the previous instance. ON/OFF ordering must span both instances.
_CLOUD_COMMAND_LOCK = threading.Lock()
_CLOUD_INIT_LOCK = threading.Lock()
_CLOUD_OFF_LOCKS_GUARD = threading.Lock()
_CLOUD_OFF_LOCKS: dict[str, threading.Lock] = {}

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
        True iff an OFF was required but ultimately failed. This includes an
        acknowledged ON and an ambiguous ON timeout/exception; caller should
        publish a ``deterrent:stuck`` event and surface it to the operator.
    error:
        Human-readable description of the failure, if any.
    on_ack_ms:
        Cloud ack latency for the ON command, in ms. ``None`` if ON raised.
    off_attempts:
        Number of OFF attempts made (1 = single success, >1 = retries).
        0 if ON was definitively rejected and OFF was never attempted.
    """

    on_success: bool
    off_success: bool | None
    error: str | None
    on_ack_ms: float | None
    off_attempts: int
    cancelled: bool = False

    @property
    def success(self) -> bool:
        """Fully successful iff both ON and OFF succeeded."""
        return self.on_success and self.off_success is True

    @property
    def stuck(self) -> bool:
        """OFF was attempted but failed, so physical state is unsafe/unknown."""
        return self.off_success is False


class TuyaCloudController:
    """Controls Tuya devices via the Cloud API.

    Uses ``tinytuya.Cloud`` which makes signed HTTPS REST calls to Tuya's
    OpenAPI.  Token refresh is handled automatically by the library.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        api_region: str = "us",
        activation_leases: RedisActivationLeases | None = None,
    ) -> None:
        self._cloud = self._bounded_factory(
            lambda: tinytuya.Cloud(
                apiRegion=api_region,
                apiKey=api_key,
                apiSecret=api_secret,
            ),
        )
        self._lock = _CLOUD_COMMAND_LOCK
        # OFF must never queue behind a stalled status poll or ON request.
        # This lock also spans credential-hot-reloaded controller instances.
        self.last_off_deadline_started_at: float | None = None
        self._safety_only = False
        self._retired_lock = threading.Lock()
        self._retired_off_routes: dict[str, TuyaCloudController] = {}
        self._activation_leases = activation_leases
        # Busy tracking - device_id → True while activate_device is running for
        # that device. Consulted by the reconciliation loop so it doesn't
        # race a legitimate in-flight actuation.
        self._busy_lock = threading.Lock()
        self._busy: set[str] = set()
        logger.info("Tuya Cloud controller initialised (region=%s)", api_region)

    @staticmethod
    def _bounded_factory(factory: Callable[[], Any]) -> Any:
        """Construct TinyTuya without allowing eager token work to hang startup."""
        if not _CLOUD_INIT_LOCK.acquire(blocking=False):
            raise TimeoutError("Previous Tuya initialisation is still pending")
        outcome: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                outcome.put((True, factory()))
            except BaseException as exc:
                outcome.put((False, exc))
            finally:
                _CLOUD_INIT_LOCK.release()

        try:
            threading.Thread(target=invoke, name="tuya-init", daemon=True).start()
        except BaseException:
            _CLOUD_INIT_LOCK.release()
            raise
        try:
            succeeded, value = outcome.get(timeout=CLOUD_CALL_TIMEOUT_SEC)
        except queue.Empty as exc:
            raise TimeoutError(
                f"Tuya initialisation exceeded {CLOUD_CALL_TIMEOUT_SEC:.1f}s",
            ) from exc
        if not succeeded:
            assert isinstance(value, BaseException)
            raise value
        return value

    def dp_code_for(self, device: DeviceConfig) -> str:
        """Return the DP code to use for on/off toggling."""
        return device.dp_code or _DEFAULT_DP_CODES.get(device.type, "switch_1")

    def restrict_to_safety_operations(self) -> None:
        """Disable ON while retaining status/OFF for removed-device cleanup."""
        self._safety_only = True

    def retain_off_route(
        self,
        device_id: str,
        previous: TuyaCloudController,
    ) -> None:
        """Route safety OFF for an old-credential device until acknowledged."""
        if previous is self:
            return
        with self._retired_lock:
            self._retired_off_routes[device_id] = previous

    # Back-compat alias - older callers may still use the underscore name.
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
            # aliases - try the common ones before giving up.
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
        should_continue: Callable[[], bool] | None = None,
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

        if self._safety_only:
            return ActivationResult(
                on_success=False,
                off_success=None,
                error="Controller retained for safety OFF only",
                on_ack_ms=None,
                off_attempts=0,
                cancelled=True,
            )

        duration_sec = clamp_duration(
            duration_sec,
            max_sec=MAX_ACTUATION_SEC,
            default=DEFAULT_TEST_FIRE_SEC,
        )

        with self._busy_lock:
            self._busy.add(device.device_id)

        try:
            return self._activate_device_inner(
                device,
                duration_sec,
                dp_code,
                request_id=request_id,
                event_type=event_type,
                should_continue=should_continue,
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
        should_continue: Callable[[], bool] | None,
    ) -> ActivationResult:
        lease: ActivationLease | None = None
        if self._activation_leases is not None:
            try:
                lease = self._activation_leases.arm(device.device_id, duration_sec)
            except Exception as exc:
                logger.error("Activation lease failed; refusing ON for %s: %s", device.name, exc)
                return ActivationResult(
                    on_success=False,
                    off_success=None,
                    error="Independent OFF watchdog lease could not be recorded",
                    on_ack_ms=None,
                    off_attempts=0,
                    cancelled=True,
                )

        # Arm the in-process OFF deadline before attempting ON. An ON request can be
        # applied by the cloud even when its response is lost, so starting a
        # timer only after an acknowledgement leaves the dangerous case open.
        self.last_off_deadline_started_at = time.monotonic()
        watchdog = threading.Timer(
            MAX_ACTUATION_SEC,
            self._watchdog_fire,
            args=(device, dp_code, request_id),
        )
        watchdog.daemon = True
        watchdog.start()

        # --- ON ---
        t_on = time.monotonic()
        try:
            on_cmd: dict[str, Any] = {"commands": [{"code": dp_code, "value": True}]}
            if should_continue is not None and not should_continue():
                watchdog.cancel()
                return ActivationResult(
                    on_success=False,
                    off_success=None,
                    error="Activation cancelled before ON",
                    on_ack_ms=None,
                    off_attempts=0,
                    cancelled=True,
                )
            on_deadline = time.monotonic() + CLOUD_CALL_TIMEOUT_SEC

            def cancelled() -> bool:
                return self._safety_only or (should_continue is not None and not should_continue())

            def send_on() -> dict[str, Any]:
                # Admission may have waited behind status. Never send a
                # generation that emergency OFF has already cancelled.
                if cancelled() or time.monotonic() >= on_deadline:
                    return {"success": False, "cancelled": True}
                try:
                    return self._cloud.sendcommand(device.device_id, on_cmd)
                finally:
                    # Runs in the actual cloud worker, even if the bounded
                    # caller and its watchdog finished long ago.
                    if cancelled() or time.monotonic() >= on_deadline:
                        self._send_off_with_retry(
                            device,
                            dp_code,
                            request_id=request_id,
                        )

            result = self._bounded_cloud_call("ON", self._lock, send_on)
            on_ack_ms = (time.monotonic() - t_on) * 1000.0
            if not result.get("success"):
                watchdog.cancel()
                msg = f"ON failed: {result}"
                logger.error(
                    "Device %s (%s) - %s [rid=%s type=%s]",
                    device.name,
                    device.device_id,
                    msg,
                    request_id,
                    event_type,
                )
                return ActivationResult(
                    on_success=False,
                    off_success=None,
                    error=msg,
                    on_ack_ms=on_ack_ms,
                    off_attempts=0,
                    cancelled=bool(result.get("cancelled")),
                )
            logger.info(
                "Device %s ON (dp=%s) cloud_ack=%.0fms [rid=%s type=%s]",
                device.name,
                dp_code,
                on_ack_ms,
                request_id,
                event_type,
            )
        except Exception as exc:
            msg = f"ON exception: {exc}"
            logger.error(
                "Device %s (%s) - %s [rid=%s type=%s]",
                device.name,
                device.device_id,
                msg,
                request_id,
                event_type,
            )
            # Timeout/exception is ambiguous: Tuya may have applied ON before
            # the response disappeared. Always drive the independent OFF lane.
            off_success, off_error, off_attempts = self._send_off_with_retry(
                device,
                dp_code,
                request_id=request_id,
            )
            # Keep the pre-ON watchdog armed even if this immediate OFF was
            # acknowledged. The abandoned ON call can still complete later
            # and re-energise the device after that acknowledgement.
            return ActivationResult(
                on_success=False,
                off_success=off_success,
                error=msg if off_success else f"{msg}; {off_error}",
                on_ack_ms=None,
                off_attempts=off_attempts,
            )

        off_success = False
        try:
            if not cancelled():
                time.sleep(duration_sec)
            off_success, off_error, off_attempts = self._send_off_with_retry(
                device,
                dp_code,
                request_id=request_id,
            )
        finally:
            if off_success:
                watchdog.cancel()

        if off_success:
            try:
                if self._activation_leases is not None and lease is not None:
                    self._activation_leases.clear(lease)
            except Exception:
                logger.warning("Could not clear activation lease for %s", device.name)
            return ActivationResult(
                on_success=True,
                off_success=True,
                error=None,
                on_ack_ms=on_ack_ms,
                off_attempts=off_attempts,
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
        """Send OFF to *device* with retries - used by the reconciliation loop
        and the admin emergency-off endpoint.

        Returns ``(ok, error_message)``. Unlike :meth:`activate_device`,
        this does not start a watchdog - the caller invokes it precisely
        to recover from a stuck state.
        """
        with self._retired_lock:
            previous = self._retired_off_routes.get(device.device_id)
        if previous is not None:
            ok, err = previous.force_off(device, request_id=request_id)
            if ok:
                with self._retired_lock:
                    self._retired_off_routes.pop(device.device_id, None)
            return ok, err

        dp_code = self._dp_code_for(device)
        ok, err, _ = self._send_off_with_retry(
            device,
            dp_code,
            request_id=request_id,
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
                # The caller owns OFF admission only for the bounded wait; the
                # abandoned cloud worker does not own it. Thus a timed-out
                # attempt cannot poison every retry and future safety sweep.
                deadline = time.monotonic() + CLOUD_CALL_TIMEOUT_SEC
                off_lock = self._off_lock_for(device.device_id)
                if not off_lock.acquire(timeout=CLOUD_CALL_TIMEOUT_SEC):
                    raise TimeoutError("OFF admission exceeded cloud-call budget")
                try:
                    result = self._bounded_cloud_call(
                        "OFF",
                        None,
                        lambda: self._cloud.sendcommand(device.device_id, off_cmd),
                        timeout_sec=max(0.0, deadline - time.monotonic()),
                    )
                finally:
                    off_lock.release()
                if result.get("success"):
                    if attempt_idx == 0:
                        logger.info(
                            "Device %s OFF [rid=%s]",
                            device.name,
                            request_id,
                        )
                    else:
                        logger.warning(
                            "Device %s OFF succeeded on retry %d [rid=%s]",
                            device.name,
                            attempts,
                            request_id,
                        )
                    return True, None, attempts
                last_error = f"OFF returned non-success: {result}"
            except Exception as exc:
                last_error = f"OFF exception: {exc}"
            logger.error(
                "Device %s OFF attempt %d/%d failed - %s [rid=%s]",
                device.name,
                attempts,
                total_attempts,
                last_error,
                request_id,
            )

        return False, f"OFF_FAILED:{device.device_id}: {last_error}", attempts

    @staticmethod
    def _off_lock_for(device_id: str) -> threading.Lock:
        """Return the process-wide OFF lane for one physical device."""
        with _CLOUD_OFF_LOCKS_GUARD:
            return _CLOUD_OFF_LOCKS.setdefault(device_id, threading.Lock())

    def _watchdog_fire(
        self,
        device: DeviceConfig,
        dp_code: str,
        request_id: str | None,
    ) -> None:
        """Fires MAX_ACTUATION_SEC after ON send if the normal OFF path hasn't
        cancelled the timer. Unconditional force-OFF backstop."""
        logger.critical(
            "WATCHDOG - device %s (%s) exceeded %.1fs - forcing OFF [rid=%s]",
            device.name,
            device.device_id,
            MAX_ACTUATION_SEC,
            request_id,
        )
        try:
            self._send_off_with_retry(device, dp_code, request_id=request_id)
        except Exception:
            logger.exception(
                "Watchdog force-OFF raised for %s (%s) [rid=%s]",
                device.name,
                device.device_id,
                request_id,
            )

    def get_device_status(self, device_id: str) -> dict[str, Any] | None:
        """Query device status (battery level, switch state, etc.).

        Returns the parsed status dict on success, or ``None`` on failure.
        """
        try:
            result = self._bounded_cloud_call(
                "status",
                self._lock,
                lambda: self._cloud.getstatus(device_id),
            )
            if result.get("success") and result.get("result"):
                return {item["code"]: item["value"] for item in result["result"]}
            logger.warning("Status query failed for %s: %s", device_id, result)
            return None
        except Exception as exc:
            logger.warning(
                "DEVICE STATUS UNKNOWN for %s: %s; physical state cannot be verified",
                device_id,
                exc,
            )
            return None

    @staticmethod
    def _bounded_cloud_call(
        operation: str,
        lock: threading.Lock | None,
        call: Callable[[], dict[str, Any]],
        *,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        """Run one TinyTuya operation with a strict wall-clock bound.

        TinyTuya performs token acquisition and refresh inside these calls, so
        the bound covers both authentication retries and the eventual API
        request. A timed-out daemon may remain stuck in third-party code, but
        it cannot retain the caller or the independent OFF lane.
        """
        outcome: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
        budget = CLOUD_CALL_TIMEOUT_SEC if timeout_sec is None else timeout_sec
        deadline = time.monotonic() + budget

        # Acquire general-lane admission before creating a worker. If an old
        # status/ON worker is permanently stuck, later callers time out here
        # without accumulating one blocked daemon per poll until pids_limit.
        if lock is not None and not lock.acquire(timeout=budget):
            raise TimeoutError(
                f"{operation} cloud lane remained busy for {CLOUD_CALL_TIMEOUT_SEC:.1f}s",
            )

        def invoke() -> None:
            try:
                outcome.put((True, call()))
            except BaseException as exc:
                outcome.put((False, exc))
            finally:
                if lock is not None:
                    lock.release()

        worker = threading.Thread(
            target=invoke,
            name=f"tuya-{operation.lower()}",
            daemon=True,
        )
        try:
            worker.start()
        except BaseException:
            if lock is not None:
                lock.release()
            raise
        try:
            remaining = max(0.0, deadline - time.monotonic())
            succeeded, value = outcome.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError(
                f"{operation} cloud call exceeded {CLOUD_CALL_TIMEOUT_SEC:.1f}s",
            ) from exc
        if not succeeded:
            assert isinstance(value, BaseException)
            raise value
        if not isinstance(value, dict):
            raise TypeError(f"{operation} cloud call returned a non-object response")
        return value
