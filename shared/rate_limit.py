"""Redis-backed per-principal rate limiting.

Uses a fixed-window counter. The counter increment and its TTL are applied
in one Lua script so a crash between ``INCR`` and ``EXPIRE`` can never leave
a window that neither rolls over nor expires, and the script also repairs a
counter that somehow lost its TTL.

The limiter fails closed: if Redis is unreachable, refuses the command
(ACL ``NOPERM``) or is out of memory (``OOM`` under ``noeviction``), the
request is denied with ``Retry-After`` set to the window. Quota state lives
in a non-evicting Redis (``maxmemory-policy noeviction``), so a counter can
only disappear by expiring at the end of its window.
"""

from __future__ import annotations

import logging
from typing import Any

import redis as redis_lib

logger = logging.getLogger(__name__)

KEY_PREFIX = "rl:v1"

# KEYS[1] = counter key, ARGV[1] = window seconds.
# Returns {count, ttl_seconds}. The TTL is (re)applied atomically with the
# first increment of a window and whenever the key has no expiry.
INCR_WITH_TTL_SCRIPT = (
    "local count = redis.call('INCR', KEYS[1]) "
    "local ttl = redis.call('TTL', KEYS[1]) "
    "if count == 1 or ttl < 0 then "
    "redis.call('EXPIRE', KEYS[1], ARGV[1]) "
    "ttl = tonumber(ARGV[1]) "
    "end "
    "return {count, ttl}"
)


class RateLimiter:
    """Fixed-window counter backed by Redis.

    Windows are absolute wall-clock, not sliding - at most ``2 * capacity``
    requests over a 2-window cusp is possible in the worst case, which is
    acceptable.
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
            raw: Any = self._redis.eval(INCR_WITH_TTL_SCRIPT, 1, key, window_seconds)
            count = int(raw[0])
            ttl = int(raw[1])
        except (redis_lib.RedisError, ValueError, TypeError, IndexError) as exc:
            logger.error(
                "Rate limiter Redis error (fail-closed, denying %s/%s): %s",
                scope, principal, exc,
            )
            return False, window_seconds

        if count <= capacity:
            return True, 0
        return False, ttl if ttl > 0 else window_seconds
