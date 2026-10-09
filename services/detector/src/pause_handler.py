"""Pause/resume handler for the detector - yields GPU to the trainer.

Subscribes to Redis ``scarguard:detector:command`` and manages the
lifecycle: set the paused flag → drain in-flight inference → unload
models → ack via Redis key.  On resume (or heartbeat timeout), reload
models and clear the flag.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable
from typing import Any

from atomic_ref import AtomicRef
from model_pool import ModelPool
from pause_protocol import (
    COMMAND_CHANNEL,
    DEFAULT_PAUSE_TIMEOUT,
    HEARTBEAT_KEY,
    MAX_PAUSE_TIMEOUT,
    STATE_KEY,
    STATE_TTL,
)
from redis_client import make_sync_client, reconnect_loop

logger = logging.getLogger(__name__)

_DRAIN_WAIT = 2.0  # seconds to wait for in-flight inference after setting paused

# Signing key for pause/resume commands (trainer → detector).
_CMD_KEY: bytes | None = None
_CMD_CACHE: object = None
_CHANNEL_FIELD: str | None = None
_VERIFY_EVENT = None
_DERIVE_CMD_KEY: "Callable[[bytes, str], bytes] | None" = None

try:
    from event_signing import (
        CHANNEL_FIELD as _EF_CHANNEL_FIELD,
    )
    from event_signing import (
        _ReplayCache,
        load_key_from_env,
    )
    from event_signing import (
        derive_channel_key as _EF_DK,
    )
    from event_signing import (
        verify_event as _EF_VERIFY_EVENT,
    )
    _CMD_KEY = load_key_from_env()
    _CMD_CACHE = _ReplayCache(capacity=4096, ttl_seconds=60) if _CMD_KEY else None
    _CHANNEL_FIELD = _EF_CHANNEL_FIELD
    _VERIFY_EVENT = _EF_VERIFY_EVENT
    _DERIVE_CMD_KEY = _EF_DK
except ImportError:
    pass


def _verify_command(payload: dict[str, Any]) -> bool:
    """Return True if *payload* is a valid signed envelope.

    When no signing key is configured the command is accepted (migration
    window).  When the key is present the payload must carry a valid,
    unique, timestamped signature for COMMAND_CHANNEL.
    """
    if _CMD_KEY is None or _CHANNEL_FIELD is None:
        return True
    channel_key = _DERIVE_CMD_KEY(_CMD_KEY, COMMAND_CHANNEL) if _DERIVE_CMD_KEY else _CMD_KEY
    if not isinstance(payload.get(_CHANNEL_FIELD), str):
        return _VERIFY_EVENT(payload, channel_key, COMMAND_CHANNEL, _CMD_CACHE)  # type: ignore[arg-type]
    if payload[_CHANNEL_FIELD] != COMMAND_CHANNEL:
        return False
    return _VERIFY_EVENT(payload, channel_key, COMMAND_CHANNEL, _CMD_CACHE)  # type: ignore[arg-type]


class PauseHandler:
    """Listens for pause/resume commands and manages detector GPU state."""

    def __init__(
        self,
        redis_cfg: dict[str, Any],
        model_pool: ModelPool,
        paused_ref: AtomicRef[bool],
        global_stop: threading.Event,
    ) -> None:
        self._redis_cfg = redis_cfg
        self._model_pool = model_pool
        self._paused_ref = paused_ref
        self._global_stop = global_stop
        self._listener_thread: threading.Thread | None = None
        self._watcher_thread: threading.Thread | None = None
        self._watcher_stop = threading.Event()
        self._paused_since: float = 0.0
        self._pause_timeout: float = DEFAULT_PAUSE_TIMEOUT
        self._pause_request_id: str = ""
        self._transition_lock = threading.Lock()

    def start(self) -> None:
        self._listener_thread = threading.Thread(
            target=self._listen,
            name="pause-handler",
            daemon=True,
        )
        self._listener_thread.start()
        self._publish_state("running")

    def stop(self) -> None:
        self._watcher_stop.set()
        if self._watcher_thread and self._watcher_thread.is_alive():
            self._watcher_thread.join(timeout=5)

    def _listen(self) -> None:
        reconnect_loop(
            self._redis_cfg,
            [COMMAND_CHANNEL],
            self._handle_command,
            self._global_stop,
            log=logger,
        )

    def _handle_command(self, _channel: str, payload: dict[str, Any]) -> None:
        if not _verify_command(payload):
            logger.warning("Rejected invalid pause/resume command from Redis")
            return
        action = payload.get("action")
        request_id = payload.get("request_id", "?")
        if action == "pause":
            try:
                timeout = float(payload.get("timeout", DEFAULT_PAUSE_TIMEOUT))
                if not math.isfinite(timeout) or not 0 < timeout <= MAX_PAUSE_TIMEOUT:
                    raise ValueError("timeout is outside the supported range")
            except (TypeError, ValueError):
                logger.warning("Invalid pause timeout in request %s", request_id)
                return
            self._do_pause(request_id, timeout)
        elif action == "resume":
            self._do_resume(request_id, reason="command")
        else:
            logger.warning("Unknown pause command action: %s", action)

    def _do_pause(self, request_id: str, timeout: float) -> None:
        with self._transition_lock:
            if self._paused_ref.get():
                logger.info("Already paused - ignoring duplicate pause request %s", request_id)
                return

            logger.info("Pausing detector (request_id=%s, timeout=%ds)", request_id, timeout)
            self._paused_since = time.monotonic()
            self._pause_timeout = float(timeout)
            self._pause_request_id = request_id
            self._paused_ref.set(True)

            self._global_stop.wait(_DRAIN_WAIT)
            if self._global_stop.is_set():
                return

            self._model_pool.unload_all()
            self._publish_state("paused", request_id=request_id)
            self._start_heartbeat_watcher()
            logger.info("Detector paused - GPU released")

    def _do_resume(self, request_id: str, reason: str = "command") -> None:
        with self._transition_lock:
            if not self._paused_ref.get():
                # Still ack: after an auto-resume the trainer's own resume
                # request must not time out waiting for a state it can never
                # see - republish "running" under the requester's id.
                logger.info("Not paused - acking resume request %s as running", request_id)
                self._publish_state("running", request_id=request_id)
                return

            logger.info("Resuming detector (request_id=%s, reason=%s)", request_id, reason)
            if threading.current_thread() != self._watcher_thread:
                self._stop_heartbeat_watcher()
            else:
                self._watcher_stop.set()
            try:
                self._model_pool.reload_all()
            except Exception:
                logger.exception("Model reload failed during resume - inference will retry on next frame")
            self._paused_ref.set(False)
            self._publish_state("running", request_id=request_id)
            logger.info("Detector resumed - inference active")

    def _publish_state(self, state: str, request_id: str | None = None) -> None:
        data: dict[str, Any] = {
            "state": state,
            "since": time.time(),
        }
        if request_id:
            data["request_id"] = request_id
        client = make_sync_client(self._redis_cfg)
        try:
            client.set(STATE_KEY, json.dumps(data), ex=STATE_TTL)
        except Exception:
            logger.exception("Failed to publish detector state")
        finally:
            client.close()

    # ── Heartbeat watcher ────────────────────────────────────────────────

    def _start_heartbeat_watcher(self) -> None:
        if self._watcher_thread and self._watcher_thread.is_alive():
            self._watcher_stop.set()
            self._watcher_thread.join(timeout=5)
        self._watcher_stop = threading.Event()
        self._watcher_thread = threading.Thread(
            target=self._heartbeat_watcher_loop,
            name="heartbeat-watcher",
            daemon=True,
        )
        self._watcher_thread.start()

    def _stop_heartbeat_watcher(self) -> None:
        self._watcher_stop.set()
        if self._watcher_thread and self._watcher_thread.is_alive():
            self._watcher_thread.join(timeout=5)
        self._watcher_thread = None

    def _heartbeat_watcher_loop(self) -> None:
        """Poll the trainer heartbeat and pause timeout while paused.

        Auto-resumes the detector if the trainer heartbeat expires (trainer
        crashed) or the pause timeout is exceeded.
        """
        try:
            client = make_sync_client(self._redis_cfg)
        except Exception:
            logger.exception("Heartbeat watcher failed to connect to Redis")
            return
        try:
            while not self._watcher_stop.is_set() and not self._global_stop.is_set():
                if not self._paused_ref.get():
                    break

                elapsed = time.monotonic() - self._paused_since
                if elapsed > self._pause_timeout:
                    logger.warning(
                        "Pause timeout exceeded (%.0fs > %.0fs) - auto-resuming",
                        elapsed, self._pause_timeout,
                    )
                    self._do_resume("auto-timeout", reason="timeout")
                    break

                heartbeat = client.get(HEARTBEAT_KEY)
                if heartbeat is None and elapsed > 60:
                    logger.warning("Trainer heartbeat missing - auto-resuming")
                    self._do_resume("auto-heartbeat", reason="heartbeat-expired")
                    break

                # Refresh the state key: a pause outlives STATE_TTL, and
                # letting it expire shows the UI "unknown" for the rest of
                # a long training run.
                self._publish_state("paused", request_id=self._pause_request_id)

                self._watcher_stop.wait(30)
        except Exception:
            logger.exception("Heartbeat watcher error - auto-resuming")
            try:
                self._do_resume("auto-error", reason="watcher-error")
            except Exception:
                logger.exception("Failed to auto-resume after watcher error")
        finally:
            try:
                client.close()
            except Exception:
                pass
