"""Live feed page — SSE stream pushes latest annotated snapshot on each detection."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config_store
import db
import redis.asyncio as aioredis
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/feed")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

CHANNEL = "scarguard:detections"


def _to_local(iso_str: str, tz_name: str) -> str:
    """Convert a UTC ISO 8601 string to a formatted local-time string."""
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError, TypeError):
        tz = ZoneInfo("UTC")
    try:
        dt = datetime.fromisoformat(iso_str).astimezone(tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, TypeError):
        return str(iso_str)


def _tz_name() -> str:
    return config_store.load_cached().get("system", {}).get("timezone") or "UTC"


@router.get("", response_class=HTMLResponse)
async def feed_page(request: Request):
    cfg = config_store.load_cached()
    cameras = cfg.get("cameras", [])
    latest = db.get_latest_event()
    tz_name = cfg.get("system", {}).get("timezone") or "UTC"
    latest_dict = None
    if latest:
        latest_dict = dict(latest)
        latest_dict["display_timestamp"] = _to_local(
            str(latest_dict.get("timestamp", "")), tz_name
        )
    return templates.TemplateResponse(
        request,
        "feed.html",
        {
            "cameras": cameras,
            "latest": latest_dict,
        },
    )


@router.get("/stream")
async def feed_stream(request: Request):
    """
    SSE stream — pushes an HTML <img> fragment for each new detection.
    The browser swaps it into the feed container via HTMX hx-swap-oob or
    a simple EventSource listener in the template.
    """
    redis_cfg = config_store.load_cached().get("redis", {})
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))

    async def generator():
        client = aioredis.Redis(host=host, port=port, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe(CHANNEL)
        yield ": connected\n\n"
        try:
            while not await request.is_disconnected():
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=15.0,
                )
                if message is None:
                    yield ": keepalive\n\n"
                    continue
                if message["type"] != "message":
                    continue
                try:
                    event = json.loads(message["data"])
                except json.JSONDecodeError:
                    continue

                snap = event.get("snapshot_path")
                tz = _tz_name()
                display_ts = _to_local(str(event.get("timestamp", "")), tz)
                label = (
                    f'{event.get("class_name","").replace("_"," ").title()} '
                    f'@ {event.get("confidence",0):.0%} — '
                    f'{event.get("camera_name","")} — '
                    f'{display_ts}'
                )
                payload = json.dumps({
                    "snapshot_filename": Path(snap).name if snap else None,
                    "class_name": event.get("class_name", ""),
                    "label": label,
                    "bbox": event.get("bbox"),
                    "frame_size": event.get("frame_size"),
                    "confidence": event.get("confidence", 0),
                })
                yield f"event: detection\ndata: {payload}\n\n"
        finally:
            await pubsub.unsubscribe(CHANNEL)
            await client.aclose()

    return StreamingResponse(generator(), media_type="text/event-stream")
