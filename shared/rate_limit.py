"""Simple Redis-backed per-principal rate limiting.

Uses a fixed-window counter with atomic ``INCR`` + ``EXPIRE`` rather than
a true token bucket. The goal here is defence against session-theft
hammering and misbehaving scripts — not precise QoS — so a fixed window
is fine and avoids the Lua complexity of a sliding window or bucket.

The limiter degrades open on Redis failure: if the Redis call raises, the
request is allowed and a warning is logged. Better to let legitimate
traffic through during a Redis outage than to hard-block the UI.
"""

from __future__ import annotations

import logging
from typing import Any

import redis as redis_lib

logger = logging.getLogger(__name__)

KEY_PREFIX = "rl:v1"


class RateLimiter:
    """Fixed-window counter backed by Redis.

    Each ``check()`` call is an ``INCR``; when the count first crosses 1
    we set ``EXPIRE`` on the key so it rolls over. Windows are absolute
    wall-clock, not sliding — at most ``2 * capacity`` requests over a
    2-window cusp is possible in the worst case, which is acceptable.
    """

    def __init__(self, redis_client: redis_lib.Redis) -> None:
        self._redis = redis_client

    def check(
        self,
        principal: str,
        scope: str,
        capacity: int,
        window_seconds: int,
    ) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)``.

        *principal* is typically ``user:<id>`` or ``ip:<addr>``. *scope* is a
        short identifier for the endpoint family (``test-fire``, ``arm``,
        etc.). *capacity* is the max requests per *window_seconds*.
        """
        if capacity <= 0 or window_seconds <= 0:
            return True, 0

        key = f"{KEY_PREFIX}:{scope}:{principal}"
        try:
            raw: Any = self._redis.incr(key)
            count = int(raw) if isinstance(raw, (int, float, str, bytes)) else -1
            if count < 0:
                return True, 0
            if count == 1:
                # First hit of this window — set the TTL.
                self._redis.expire(key, window_seconds)
        except (redis_lib.RedisError, ValueError, TypeError) as exc:
            logger.warning("Rate limiter Redis error (fail-open): %s", exc)
            return True, 0

        if count <= capacity:
            return True, 0

        # Over the limit — read the TTL for Retry-After. TTL can briefly be
        # -1 (no expiry yet) or -2 (no key); clamp conservatively.
        try:
            ttl_raw: Any = self._redis.ttl(key)
            ttl = int(ttl_raw) if isinstance(ttl_raw, (int, float)) else window_seconds
        except redis_lib.RedisError:
            ttl = window_seconds
        retry_after = ttl if ttl > 0 else window_seconds
        return False, retry_after
