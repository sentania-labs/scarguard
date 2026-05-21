"""Shared Redis client helpers.

Provides factory functions and a reconnect loop that standardise the
Redis-client patterns duplicated across detector, notifier, deterrent,
backup, and log-streamer.  Services can adopt these incrementally.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable

import redis as redis_lib

logger = logging.getLogger(__name__)

_MIN_RECONNECT_DELAY: float = 5
_MAX_RECONNECT_DELAY: float = 60


def _resolve_params(redis_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build common connection kwargs from a config dict + env."""
    cfg = redis_cfg or {}
    return {
        "host": cfg.get("host", "redis"),
        "port": int(cfg.get("port", 6379)),
        "password": os.environ.get("REDIS_PASSWORD", "") or None,
        "decode_responses": True,
    }


def make_sync_client(
    redis_cfg: dict[str, Any] | None = None,
    *,
    socket_connect_timeout: int = 5,
    socket_timeout: int | None = None,
    retry_on_timeout: bool = True,
    **extra: Any,
) -> redis_lib.Redis:
    """Create a configured synchronous Redis client.

    Sensible defaults: 5s connect timeout, ``retry_on_timeout=True``.
    Extra kwargs are forwarded to :class:`redis.Redis`.
    """
    params = _resolve_params(redis_cfg)
    params.update(
        socket_connect_timeout=socket_connect_timeout,
        retry_on_timeout=retry_on_timeout,
    )
    if socket_timeout is not None:
        params["socket_timeout"] = socket_timeout
    params.update(extra)
    return redis_lib.Redis(**params)


def make_async_client(
    redis_cfg: dict[str, Any] | None = None,
    **extra: Any,
) -> Any:
    """Create an async Redis client (``redis.asyncio.Redis``).

    Import is deferred so services that don't use async don't pay for it.
    """
    import redis.asyncio as aioredis

    params = _resolve_params(redis_cfg)
    params.update(extra)
    return aioredis.Redis(**params)


def reconnect_loop(
    redis_cfg: dict[str, Any],
    channels: list[str],
    handler: Callable[[str, dict[str, Any]], None],
    shutdown: threading.Event,
    *,
    log: logging.Logger | None = None,
    health_path: str | None = None,
) -> None:
    """Common sync pubsub reconnect loop with exponential backoff.

    Subscribes to *channels*, deserialises each message as JSON, and
    calls ``handler(channel, payload)`` for every ``message``-type event.
    Reconnects on :class:`redis.RedisError` with exponential backoff from
    5 s to 60 s.  Exits cleanly when *shutdown* is set.

    If *health_path* is given, touches the file after every successful
    subscribe and every received message so Docker healthchecks can detect
    liveness.
    """
    import json
    import pathlib

    _log = log or logger
    delay = _MIN_RECONNECT_DELAY

    while not shutdown.is_set():
        client: redis_lib.Redis | None = None
        pubsub: redis_lib.client.PubSub | None = None
        try:
            client = make_sync_client(redis_cfg)
            pubsub = client.pubsub()
            pubsub.subscribe(*channels)
            _log.info("Subscribed to Redis channel(s): %s", ", ".join(channels))
            delay = _MIN_RECONNECT_DELAY

            if health_path:
                pathlib.Path(health_path).touch(exist_ok=True)

            for message in pubsub.listen():
                if shutdown.is_set():
                    break
                if message["type"] != "message":
                    continue
                if health_path:
                    pathlib.Path(health_path).touch(exist_ok=True)
                try:
                    payload = json.loads(message["data"])
                except json.JSONDecodeError:
                    _log.warning("Malformed message on %s: %s", message["channel"], message["data"])
                    continue
                handler(message["channel"], payload)

        except redis_lib.RedisError:
            if shutdown.is_set():
                break
            _log.exception("Redis connection lost — retrying in %ds", delay)
            shutdown.wait(delay)
            delay = min(delay * 2, _MAX_RECONNECT_DELAY)
        finally:
            if pubsub is not None:
                try:
                    pubsub.unsubscribe()
                    pubsub.close()
                except Exception:
                    pass
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    _log.info("Reconnect loop stopped")
