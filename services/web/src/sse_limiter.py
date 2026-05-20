"""SSE concurrent-connection limiter.

Tracks active SSE streams per principal using a Redis SET with a TTL
failsafe.  Prevents a single user or bot from exhausting server
connections by opening many SSE streams in parallel.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

MAX_PER_USER = int(os.environ.get("SSE_MAX_PER_USER", "5"))
MAX_GLOBAL = int(os.environ.get("SSE_MAX_GLOBAL", "20"))
SET_TTL_SEC = 300
_GLOBAL_KEY = "sse:connections:__global__"


def _user_key(user_id: Any) -> str:
    return f"sse:connections:{user_id}"


class SSETooManyStreams(Exception):
    pass


@asynccontextmanager
async def sse_connection(
    redis_client: Any,
    user_id: Any,
) -> AsyncIterator[str]:
    """Context manager that registers/unregisters an SSE stream.

    Raises :class:`SSETooManyStreams` if the per-user or global cap is
    exceeded.  On normal exit (or exception), the stream ID is removed
    from both SETs.
    """
    stream_id = uuid.uuid4().hex
    ukey = _user_key(user_id)

    try:
        user_count = await redis_client.scard(ukey)
        global_count = await redis_client.scard(_GLOBAL_KEY)
    except Exception:
        yield stream_id
        return

    if user_count >= MAX_PER_USER:
        raise SSETooManyStreams(
            f"Too many SSE streams for user {user_id} ({user_count}/{MAX_PER_USER})"
        )
    if global_count >= MAX_GLOBAL:
        raise SSETooManyStreams(
            f"Too many global SSE streams ({global_count}/{MAX_GLOBAL})"
        )

    try:
        pipe = redis_client.pipeline()
        pipe.sadd(ukey, stream_id)
        pipe.expire(ukey, SET_TTL_SEC)
        pipe.sadd(_GLOBAL_KEY, stream_id)
        pipe.expire(_GLOBAL_KEY, SET_TTL_SEC)
        await pipe.execute()
    except Exception:
        logger.debug("Failed to register SSE stream — allowing anyway")
        yield stream_id
        return

    try:
        yield stream_id
    finally:
        try:
            pipe = redis_client.pipeline()
            pipe.srem(ukey, stream_id)
            pipe.srem(_GLOBAL_KEY, stream_id)
            await pipe.execute()
        except Exception:
            logger.debug("Failed to unregister SSE stream %s", stream_id)
