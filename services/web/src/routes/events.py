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

router = APIRouter(prefix="/events")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

PAGE_SIZE = 50
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


def _apply_display_timestamp(events: list[dict]) -> list[dict]:
    tz = _tz_name()
    for e in events:
        e["display_timestamp"] = _to_local(e.get("timestamp", ""), tz)
    return events


@router.get("", response_class=HTMLResponse)
async def events_page(request: Request, page: int = 1):
    offset = (page - 1) * PAGE_SIZE
    rows = db.get_events(limit=PAGE_SIZE, offset=offset)
    total = db.count_events()
    events = _apply_display_timestamp([dict(r) for r in rows])
    return templates.TemplateResponse(
        request,
        "events.html",
        {
            "events": events,
            "page": page,
            "total_pages": max(1, -(-total // PAGE_SIZE)),  # ceiling div
            "total": total,
        },
    )


@router.get("/rows", response_class=HTMLResponse)
async def event_rows(request: Request, page: int = 1):
    """HTMX partial — just the table body rows."""
    offset = (page - 1) * PAGE_SIZE
    rows = db.get_events(limit=PAGE_SIZE, offset=offset)
    events = _apply_display_timestamp([dict(r) for r in rows])
    return templates.TemplateResponse(
        request,
        "partials/event_rows.html",
        {"events": events},
    )


@router.get("/stream")
async def event_stream(request: Request):
    """SSE stream — pushes a new event row fragment whenever a detection fires."""
    cfg = config_store.load_cached()
    redis_cfg = cfg.get("redis", {})
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))

    async def generator():
        client = aioredis.Redis(host=host, port=port, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe(CHANNEL)
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
                tz = _tz_name()
                html = _render_event_row(event, tz)
                yield f"event: detection\ndata: {html}\n\n"
        finally:
            await pubsub.unsubscribe(CHANNEL)
            await client.aclose()

    return StreamingResponse(generator(), media_type="text/event-stream")


def _render_event_row(event: dict, tz_name: str = "UTC") -> str:
    snap = event.get("snapshot_path")
    snap_html = ""
    if snap:
        fname = Path(snap).name
        snap_html = f'<a href="/snapshots/{fname}" target="_blank"><img src="/snapshots/{fname}" width="80"></a>'
    conf = event.get("confidence", 0)
    display_ts = _to_local(event.get("timestamp", ""), tz_name)
    return (
        f'<tr id="event-live">'
        f'<td>{display_ts}</td>'
        f'<td>{event.get("class_name", "").replace("_", " ").title()}</td>'
        f'<td>{conf:.0%}</td>'
        f'<td>{event.get("camera_name", "")}</td>'
        f'<td>{snap_html}</td>'
        f'</tr>'
    )
