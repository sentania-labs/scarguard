"""A deliberately OFF-only, bounded Tuya Cloud client."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

import tinytuya
from deterrent_safety import CLOUD_CALL_TIMEOUT_SEC, OFF_RETRY_BACKOFF_SEC

logger = logging.getLogger(__name__)
_INIT_LOCK = threading.Lock()
_DEVICE_LOCKS_GUARD = threading.Lock()
_DEVICE_LOCKS: dict[str, threading.Lock] = {}


class OffOnlyCloudController:
    """Expose only status and OFF; this class has no activation operation."""

    def __init__(self, api_key: str, api_secret: str, api_region: str = "us") -> None:
        self._cloud = self._bounded(
            lambda: tinytuya.Cloud(
                apiRegion=api_region,
                apiKey=api_key,
                apiSecret=api_secret,
            ),
            _INIT_LOCK,
        )

    @staticmethod
    def _bounded(call: Callable[[], Any], admission: threading.Lock) -> Any:
        if not admission.acquire(timeout=CLOUD_CALL_TIMEOUT_SEC):
            raise TimeoutError("Previous Tuya call is still pending")
        result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                result.put((True, call()))
            except BaseException as exc:
                result.put((False, exc))
            finally:
                admission.release()

        try:
            threading.Thread(target=invoke, daemon=True).start()
        except BaseException:
            admission.release()
            raise
        try:
            succeeded, value = result.get(timeout=CLOUD_CALL_TIMEOUT_SEC)
        except queue.Empty as exc:
            raise TimeoutError("Tuya call exceeded watchdog bound") from exc
        if not succeeded:
            assert isinstance(value, BaseException)
            raise value
        return value

    def force_off(self, device_id: str, dp_code: str) -> bool:
        """Send only a false-valued switch command, with bounded retries."""
        command: dict[str, Any] = {
            "commands": [{"code": dp_code, "value": False}],
        }
        with _DEVICE_LOCKS_GUARD:
            admission = _DEVICE_LOCKS.setdefault(device_id, threading.Lock())
        for attempt in range(1 + len(OFF_RETRY_BACKOFF_SEC)):
            if attempt:
                time.sleep(OFF_RETRY_BACKOFF_SEC[attempt - 1])
            try:
                response = self._bounded(
                    lambda: self._cloud.sendcommand(device_id, command),
                    admission,
                )
                if isinstance(response, dict) and response.get("success"):
                    return True
            except Exception as exc:
                logger.warning("Bounded OFF attempt failed for %s: %s", device_id, exc)
        return False
