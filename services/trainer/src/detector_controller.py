"""Client for the narrowly allowlisted detector lifecycle controller."""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

CONTROLLER_URL = os.environ.get("TRAINING_CONTROLLER_URL", "http://training-controller:8090")


class DetectorControllerClient:
    """Own a hard detector stop lease and its recovery heartbeat."""

    def __init__(
        self,
        job_id: str,
        base_url: str = CONTROLLER_URL,
        token: str | None = None,
    ) -> None:
        self.job_id = job_id
        self.base_url = base_url.rstrip("/")
        self.token = token if token is not None else os.environ.get("TRAINING_CONTROLLER_TOKEN", "")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _post(self, action: str) -> dict[str, Any]:
        if len(self.token) < 32:
            raise RuntimeError("Training controller authentication is not configured")
        body = json.dumps({"job_id": self.job_id}).encode()
        request = Request(
            f"{self.base_url}/v1/detector/lease/{action}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Scarguard-Controller-Token": self.token,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=40) as response:  # noqa: S310 - fixed internal URL
                payload = json.loads(response.read() or b"{}")
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read() or b"{}").get("error", str(exc))
            except Exception:
                detail = str(exc)
            raise RuntimeError(f"Detector controller {action} failed: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(
                f"Detector controller unavailable during {action}: {exc.reason}"
            ) from exc
        return payload if isinstance(payload, dict) else {}

    def acquire(self) -> dict[str, Any]:
        state = self._post("acquire")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"detector-lease-{self.job_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        return state

    def release(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        return self._post("release")

    def recover(self) -> dict[str, Any]:
        return self._post("recover")

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(30):
            try:
                self._post("heartbeat")
            except Exception:
                logger.exception("Detector controller heartbeat failed")
