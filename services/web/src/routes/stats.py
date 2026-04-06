"""Admin stats page — live system resource and inference performance metrics."""

import asyncio
import csv
import io
import json
import logging
import os
from pathlib import Path

import config_store
import db
import redis.asyncio as aioredis
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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
    interval = max(1, int(cfg.get("system", {}).get("stats_interval", 5)))

    async def generator():
        client = aioredis.Redis(host=host, port=port, password=os.environ.get("REDIS_PASSWORD", "") or None, decode_responses=True)
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


# ── Historical metrics ───────────────────────────────────────────────────


def _parse_range(range_str: str) -> int:
    """Parse range string like '1h', '24h', '7d', '30d' to hours."""
    s = range_str.strip().lower()
    try:
        if s.endswith("d"):
            return int(s[:-1]) * 24
        if s.endswith("h"):
            return int(s[:-1])
    except (ValueError, IndexError):
        pass
    return 24


@router.get("/stats/history")
async def stats_history(
    request: Request,  # noqa: ARG001
    range: str = "24h",
) -> JSONResponse:
    """Return historical metrics as JSON for Chart.js."""
    hours = _parse_range(range)
    rows = db.get_metrics_for_chart(range_hours=hours)
    data: list[dict] = []
    for r in rows:
        entry: dict = {
            "timestamp": r["timestamp"],
            "cpu_pct": r["cpu_pct"],
            "gpu_pct": r["gpu_pct"],
            "gpu_temp": r["gpu_temp"],
            "ram_used_mb": r["ram_used_mb"],
            "ram_total_mb": r["ram_total_mb"],
        }
        cam_data = r["camera_data"]
        if cam_data:
            try:
                entry["cameras"] = json.loads(cam_data)
            except (json.JSONDecodeError, TypeError):
                pass
        data.append(entry)
    return JSONResponse(data)


@router.get("/stats/export")
async def stats_export(
    request: Request,  # noqa: ARG001
    range: str = "7d",
    format: str = "csv",  # noqa: A002
) -> StreamingResponse:
    """Export metrics as CSV."""
    hours = _parse_range(range)
    rows = db.get_metrics(range_hours=hours, limit=100_000)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "timestamp", "cpu_pct", "gpu_pct", "gpu_temp",
        "ram_used_mb", "ram_total_mb", "camera_data",
    ])
    for r in rows:
        writer.writerow([
            r["timestamp"], r["cpu_pct"], r["gpu_pct"], r["gpu_temp"],
            r["ram_used_mb"], r["ram_total_mb"], r["camera_data"],
        ])

    buf.seek(0)
    filename = f"scarguard_metrics_{range}.{format}"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
