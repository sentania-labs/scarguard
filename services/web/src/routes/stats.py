"""Admin stats page — live system resource and inference performance metrics."""

import asyncio
import logging
from pathlib import Path

import config_store
import redis.asyncio as aioredis
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

REDIS_KEY = "scarguard:stats"


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request) -> HTMLResponse:
    cfg = config_store.load_cached()
    interval = cfg.get("system", {}).get("stats_interval", 5)
    return templates.TemplateResponse(
        request,
        "stats.html",
        {"interval": interval},
    )


@router.get("/stats/stream")
async def stats_stream(request: Request) -> StreamingResponse:
    """SSE endpoint — polls the Redis stats key and pushes snapshots to the browser."""
    cfg = config_store.load_cached()
    redis_cfg = cfg.get("redis", {})
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))
    interval = cfg.get("system", {}).get("stats_interval", 5)

    async def generator():
        client = aioredis.Redis(host=host, port=port, decode_responses=True)
        yield ": connected\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await client.get(REDIS_KEY)
                    if data:
                        yield f"event: stats\ndata: {data}\n\n"
                    else:
                        yield "event: stats\ndata: {\"error\": \"No stats available — detector may not be running.\"}\n\n"
                except Exception:
                    logger.debug("Failed to read stats from Redis", exc_info=True)
                    yield ": redis-error\n\n"
                await asyncio.sleep(interval)
        finally:
            await client.aclose()

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
