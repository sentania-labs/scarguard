"""Actuation log — view deterrent actuation events."""

from __future__ import annotations

import html as _html
import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import actuation_db
import config_store
import redis.asyncio as aioredis
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from route_auth import require_viewer
from starlette.responses import Response

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/actuations")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

PAGE_SIZE = 50
CHANNEL = "scarguard:actuations"


def _to_local(iso_str: str, tz_name: str) -> str:
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
async def actuations_page(
    request: Request,
    page: int = 1,
    trigger_class: str = "",
    camera: str = "",
    date_from: str = "",
    date_to: str = "",
) -> Response:
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate

    offset = (page - 1) * PAGE_SIZE
    events = actuation_db.get_actuations(
        limit=PAGE_SIZE, offset=offset,
        trigger_class=trigger_class or None,
        camera=camera or None,
        date_from=date_from or None,
        date_to=date_to or None,
    )
    total = actuation_db.count_actuations(
        trigger_class=trigger_class or None,
        camera=camera or None,
        date_from=date_from or None,
        date_to=date_to or None,
    )
    total_pages = max(1, math.ceil(total / PAGE_SIZE))

    # Add display timestamps and fetch per-event actions
    tz = _tz_name()
    for ev in events:
        ev["display_ts"] = _to_local(ev.get("timestamp", ""), tz)
        ev["actions"] = actuation_db.get_actuation_actions(ev["id"])

    return templates.TemplateResponse(
        request,
        "actuations.html",
        {
            "events": events,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "trigger_class": trigger_class,
            "camera": camera,
            "date_from": date_from,
            "date_to": date_to,
        },
    )


@router.get("/stream")
async def actuations_stream(request: Request) -> StreamingResponse:
    """SSE — push new actuation rows as they happen."""
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate  # type: ignore[return-value]

    cfg = config_store.load_cached()
    redis_cfg = cfg.get("redis", {})
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))

    async def generator():
        client = aioredis.Redis(
            host=host, port=port,
            password=os.environ.get("REDIS_PASSWORD", "") or None,
            decode_responses=True,
        )
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
                html = _render_actuation_row(event)
                yield f"event: actuation\ndata: {html}\n\n"
        finally:
            await pubsub.unsubscribe(CHANNEL)
            await client.aclose()

    return StreamingResponse(generator(), media_type="text/event-stream")


def _render_actuation_row(event: dict) -> str:
    """Render a single actuation event as an HTML table row for SSE injection."""
    tz = _tz_name()
    display_ts = _to_local(event.get("timestamp", ""), tz)
    trigger_class = _html.escape(event.get("trigger_class", "").replace("_", " ").title())
    trigger_camera = _html.escape(event.get("trigger_camera", ""))
    confidence = event.get("trigger_confidence", 0.0)
    actions = event.get("actions", [])
    device_count = len(actions)
    success_count = sum(1 for a in actions if a.get("success"))
    total_dur = event.get("total_duration_sec", 0.0)

    device_names = ", ".join(
        _html.escape(a.get("device_name", "")) for a in actions
    ) or "\u2014"

    status_class = "ok" if success_count == device_count else ("warn" if success_count > 0 else "err")

    return (
        f'<tr class="actuation-new">'
        f"<td>{display_ts}</td>"
        f"<td>{trigger_class}</td>"
        f"<td>{trigger_camera}</td>"
        f"<td>{confidence:.0%}</td>"
        f"<td>{device_names}</td>"
        f'<td class="status-{status_class}">{success_count}/{device_count}</td>'
        f"<td>{total_dur:.1f}s</td>"
        f"</tr>"
    )
