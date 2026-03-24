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


@router.get("", response_class=HTMLResponse)
async def feed_page(request: Request):
    cfg = config_store.load()
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
    redis_cfg = config_store.load().get("redis", {})
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))

    async def generator():
        client = aioredis.Redis(host=host, port=port, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe(CHANNEL)
        # Send an initial keepalive comment so the connection is established
        yield ": connected\n\n"
        try:
            async for message in pubsub.listen():
                if await request.is_disconnected():
                    break
                if message["type"] != "message":
                    continue
                try:
                    event = json.loads(message["data"])
                except json.JSONDecodeError:
                    continue

                snap = event.get("snapshot_path")
                if snap:
                    fname = Path(snap).name
                    img_html = (
                        f'<img id="live-snapshot" src="/snapshots/{fname}" '
                        f'alt="{event.get("class_name", "")} detected" '
                        f'style="max-width:100%;border-radius:4px;">'
                    )
                else:
                    img_html = '<p id="live-snapshot">Detection — no snapshot.</p>'

                tz = config_store.load().get("system", {}).get("timezone") or "UTC"
                display_ts = _to_local(str(event.get("timestamp", "")), tz)
                label = (
                    f'{event.get("class_name","").replace("_"," ").title()} '
                    f'@ {event.get("confidence",0):.0%} — '
                    f'{event.get("camera_name","")} — '
                    f'{display_ts}'
                )
                payload = json.dumps({"html": img_html, "label": label})
                yield f"event: detection\ndata: {payload}\n\n"
        finally:
            await pubsub.unsubscribe(CHANNEL)
            await client.aclose()

    return StreamingResponse(generator(), media_type="text/event-stream")
