"""Pause/resume protocol constants and client for the training pipeline.

The detector subscribes to the command channel and publishes state updates.
The trainer (or any pause requester) uses :class:`PauseClient` to send
commands and wait for acknowledgement.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Redis pub/sub channel for pause/resume commands (trainer → detector).
COMMAND_CHANNEL = "scarguard:detector:command"

# Redis key where the detector publishes its current state (JSON).
STATE_KEY = "scarguard:detector:state"
STATE_TTL = 3600  # 1 hour

# Redis key written by the trainer while the detector is paused.
HEARTBEAT_KEY = "scarguard:trainer:heartbeat"
HEARTBEAT_INTERVAL = 30  # seconds between heartbeat writes
HEARTBEAT_TTL = 90  # key TTL — expires if trainer stops writing

# Default pause timeout — detector auto-resumes after this many seconds.
# This is a last-resort ceiling for a trainer that is alive (heartbeating)
# but never finishes; the 90s heartbeat TTL is the crash guard. It must
# comfortably exceed a real training run: a full prepare_and_train on the
# Orin Nano runs 10+ hours, and an auto-resume mid-training puts detector
# inference and the training subprocess on the GPU together, OOM-killing
# the training run (pond_v3, 2026-07-11).
DEFAULT_PAUSE_TIMEOUT = 86400  # 24 hours


class PauseClient:
    """Client used by the trainer to pause/resume the detector.

    Usage::

        client = PauseClient(redis_cfg)
        if client.pause():
            client.start_heartbeat()
            try:
                do_training_work()
            finally:
                client.stop_heartbeat()
                client.resume()
    """

    def __init__(self, redis_cfg: dict[str, Any]) -> None:
        self._redis_cfg = redis_cfg
        self._heartbeat_stop: threading.Event | None = None
        self._heartbeat_thread: threading.Thread | None = None

    def _make_client(self) -> Any:
        from redis_client import make_sync_client
        return make_sync_client(self._redis_cfg)

    def pause(
        self,
        timeout: int = DEFAULT_PAUSE_TIMEOUT,
        wait_timeout: float = 30.0,
    ) -> bool:
        """Send a pause command and wait for the detector to acknowledge.

        Returns True if the detector confirmed it is paused.
        """
        request_id = uuid.uuid4().hex[:12]
        client = self._make_client()
        try:
            payload = json.dumps({
                "action": "pause",
                "request_id": request_id,
                "timeout": timeout,
            })
            client.publish(COMMAND_CHANNEL, payload)
            logger.info("Pause request sent (request_id=%s, timeout=%ds)", request_id, timeout)
            return self._wait_for_state(client, "paused", request_id, wait_timeout)
        finally:
            client.close()

    def resume(self, wait_timeout: float = 30.0) -> bool:
        """Send a resume command and wait for the detector to acknowledge.

        Returns True if the detector confirmed it is running.
        """
        request_id = uuid.uuid4().hex[:12]
        client = self._make_client()
        try:
            payload = json.dumps({
                "action": "resume",
                "request_id": request_id,
            })
            client.publish(COMMAND_CHANNEL, payload)
            logger.info("Resume request sent (request_id=%s)", request_id)
            return self._wait_for_state(client, "running", request_id, wait_timeout)
        finally:
            client.close()

    def start_heartbeat(self) -> None:
        """Start a background thread that refreshes the heartbeat key."""
        if self._heartbeat_thread is not None:
            return
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="pause-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def stop_heartbeat(self) -> None:
        """Stop the heartbeat thread and remove the key."""
        if self._heartbeat_stop is not None:
            self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5)
            self._heartbeat_thread = None
        try:
            client = self._make_client()
            client.delete(HEARTBEAT_KEY)
            client.close()
        except Exception:
            pass

    def _heartbeat_loop(self) -> None:
        assert self._heartbeat_stop is not None
        try:
            client = self._make_client()
        except Exception:
            logger.exception("Heartbeat thread failed to connect to Redis")
            return
        try:
            while not self._heartbeat_stop.is_set():
                client.set(HEARTBEAT_KEY, "alive", ex=HEARTBEAT_TTL)
                self._heartbeat_stop.wait(HEARTBEAT_INTERVAL)
        except Exception:
            logger.exception("Heartbeat thread error")
        finally:
            client.close()

    def _wait_for_state(
        self,
        client: Any,
        target_state: str,
        request_id: str,
        timeout: float,
    ) -> bool:
        """Poll the state key until the target state is reached or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = client.get(STATE_KEY)
            if raw:
                try:
                    state = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    state = {}
                if (
                    state.get("state") == target_state
                    and state.get("request_id") == request_id
                ):
                    logger.info(
                        "Detector confirmed state=%s (request_id=%s)",
                        target_state, request_id,
                    )
                    return True
            time.sleep(0.5)
        logger.warning(
            "Timed out waiting for detector state=%s (request_id=%s)",
            target_state, request_id,
        )
        return False
