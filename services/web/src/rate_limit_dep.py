"""FastAPI dependency for per-endpoint rate limiting.

Wraps shared/rate_limit.py in a dependency factory that pulls the
principal from the request (session user_id > session_id > client IP) and
raises 429 with a ``Retry-After`` header on limit. Used by the physical-
control endpoints (test-fire, arm/disarm, force-off) and the abuse-prone
endpoints (config save, model upload, feedback).

The underlying Redis client is created lazily on first use so that
importing this module doesn't block on Redis availability.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

import redis as redis_lib
from fastapi import HTTPException, Request
from rate_limit import RateLimiter

logger = logging.getLogger(__name__)


_limiter: RateLimiter | None = None


def _get_limiter() -> RateLimiter | None:
    global _limiter
    if _limiter is not None:
        return _limiter
    try:
        import config_store
        redis_cfg = config_store.load_cached().get("redis", {}) or {}
        host = redis_cfg.get("host", "redis")
        port = int(redis_cfg.get("port", 6379))
        password = os.environ.get("REDIS_PASSWORD", "") or None
        client = redis_lib.Redis(
            host=host, port=port, password=password, decode_responses=True,
            socket_connect_timeout=2, socket_timeout=2,
        )
        _limiter = RateLimiter(client)
        return _limiter
    except Exception as exc:
        logger.warning(
            "Rate limiter could not be constructed (%s) — requests will pass",
            exc,
        )
        return None


def _principal(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if isinstance(user, dict):
        uid = user.get("user_id")
        if uid:
            return f"user:{uid}"
    client = request.client
    if client is not None:
        return f"ip:{client.host}"
    return "ip:unknown"


def rate_limit(scope: str, capacity: int, window_seconds: int) -> Callable[[Request], None]:
    """Return a FastAPI dependency that enforces a rate limit.

    Example::

        @router.post("/test-fire", dependencies=[Depends(rate_limit("test-fire", 10, 60))])
        async def test_fire(...): ...
    """
    def dep(request: Request) -> None:
        limiter = _get_limiter()
        if limiter is None:
            return
        principal = _principal(request)
        allowed, retry_after = limiter.check(principal, scope, capacity, window_seconds)
        if not allowed:
            logger.warning(
                "Rate limit hit [%s] by %s — retry after %ds",
                scope, principal, retry_after,
            )
            raise HTTPException(
                status_code=429,
                detail=f"Too many requests — retry in {retry_after}s",
                headers={"Retry-After": str(retry_after)},
            )
    return dep
