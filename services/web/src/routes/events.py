import html as _html
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config_store
import db
import redis.asyncio as aioredis
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sse_limiter import SSETooManyStreams, sse_connection

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
        # Deserialize JSON strings stored in SQLite.
        for key in ("actions_triggered", "bbox", "frame_size"):
            raw = e.get(key)
            if isinstance(raw, str):
                try:
                    e[key] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    e[key] = [] if key == "actions_triggered" else None
    return events


def _get_camera_names() -> list[str]:
    """Return distinct camera names from the DB for the filter dropdown."""
    try:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT camera_name FROM detection_events"
                " WHERE camera_name != '_system' ORDER BY camera_name"
            ).fetchall()
        return [r["camera_name"] for r in rows]
    except Exception:
        return []


@router.get("", response_class=HTMLResponse)
async def events_page(
    request: Request,
    page: int = 1,
    camera: str = "",
    class_name: str = "",
    date_from: str = "",
    date_to: str = "",
    feedback: str = "",
):
    offset = (page - 1) * PAGE_SIZE
    cam = camera or None
    cls = None if (not class_name or class_name.strip() == "*") else class_name
    dfrom = date_from or None
    dto = date_to or None
    fb = feedback or None
    rows = db.get_events(limit=PAGE_SIZE, offset=offset, camera=cam, class_name=cls, date_from=dfrom, date_to=dto, feedback=fb)
    total = db.count_events(camera=cam, class_name=cls, date_from=dfrom, date_to=dto, feedback=fb)
    events = _apply_display_timestamp([dict(r) for r in rows])
    return templates.TemplateResponse(
        request,
        "events.html",
        {
            "events": events,
            "page": page,
            "total_pages": max(1, -(-total // PAGE_SIZE)),  # ceiling div
            "total": total,
            "camera_names": _get_camera_names(),
            "filter_camera": camera,
            "filter_class": class_name,
            "filter_date_from": date_from,
            "filter_date_to": date_to,
            "filter_feedback": feedback,
            "target_classes": _get_target_classes(),
        },
    )


@router.get("/rows", response_class=HTMLResponse)
async def event_rows(
    request: Request,
    page: int = 1,
    camera: str = "",
    class_name: str = "",
    date_from: str = "",
    date_to: str = "",
):
    """HTMX partial — just the table body rows."""
    offset = (page - 1) * PAGE_SIZE
    rows = db.get_events(
        limit=PAGE_SIZE, offset=offset,
        camera=camera or None, class_name=class_name or None,
        date_from=date_from or None, date_to=date_to or None,
    )
    events = _apply_display_timestamp([dict(r) for r in rows])
    return templates.TemplateResponse(
        request,
        "partials/event_rows.html",
        {"events": events, "target_classes": _get_target_classes()},
    )


def _get_target_classes() -> list[str]:
    """Return target classes from config for the wrong-class dropdown."""
    return config_store.load_cached().get("detection", {}).get("target_classes", [])


@router.post("/{event_id}/feedback", response_class=HTMLResponse)
async def submit_feedback(
    request: Request,
    event_id: int,
    feedback: str = Form(...),
    corrected_class: str = Form(""),
):
    """Set or update feedback on a detection event.  Returns the updated row."""
    if feedback not in ("correct", "false_positive", "wrong_class"):
        feedback = "correct"
    corr = corrected_class.strip() or None
    if feedback != "wrong_class":
        corr = None
    if feedback == "wrong_class" and corr is None:
        # Refuse to store wrong_class without an actual corrected class
        row = db.get_event(event_id)
        if row is None:
            return HTMLResponse("<tr><td colspan='7'>Event not found</td></tr>")
        events = _apply_display_timestamp([dict(row)])
        return templates.TemplateResponse(
            request,
            "partials/event_rows.html",
            {"events": events, "target_classes": _get_target_classes()},
        )
    db.update_feedback(event_id, feedback, corr)
    row = db.get_event(event_id)
    if row is None:
        return HTMLResponse("<tr><td colspan='7'>Event not found</td></tr>")
    event = dict(row)
    events = _apply_display_timestamp([event])
    return templates.TemplateResponse(
        request,
        "partials/event_rows.html",
        {"events": events, "target_classes": _get_target_classes()},
    )


@router.get("/stream")
async def event_stream(request: Request):
    """SSE stream — pushes a new event row fragment whenever a detection fires."""
    cfg = config_store.load_cached()
    redis_cfg = cfg.get("redis", {})
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))
    user = getattr(request.state, "user", None)
    user_id = user.id if user else "anon"

    async def generator():
        client = aioredis.Redis(host=host, port=port, password=os.environ.get("REDIS_PASSWORD", "") or None, decode_responses=True)
        try:
            async with sse_connection(client, user_id):
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
                        tz = _tz_name()
                        html = _render_event_row(event, tz)
                        yield f"event: detection\ndata: {html}\n\n"
                finally:
                    await pubsub.unsubscribe(CHANNEL)
        except SSETooManyStreams:
            yield "event: error\ndata: Too many active streams\n\n"
        finally:
            await client.aclose()

    return StreamingResponse(generator(), media_type="text/event-stream")


def _render_event_row(event: dict, tz_name: str = "UTC") -> str:
    snap = event.get("snapshot_path")
    bbox = event.get("bbox")
    frame_size = event.get("frame_size")
    snap_html = ""
    if snap:
        fname = _html.escape(Path(snap).name)
        data_attrs = ""
        if bbox and frame_size:
            data_attrs = (
                f' data-bbox="{_html.escape(json.dumps(bbox))}"'
                f' data-frame-size="{_html.escape(json.dumps(frame_size))}"'
            )
        snap_html = (
            f'<a href="/snapshots/{fname}" target="_blank" class="snapshot-link"'
            f'{data_attrs}>'
            f'<img src="/snapshots/{fname}" width="80" loading="lazy">'
            f'</a>'
        )
    conf = event.get("confidence", 0)
    display_ts = _to_local(event.get("timestamp", ""), tz_name)
    actions: list[str] = event.get("actions_triggered") or []
    actions_html = (
        "".join(f'<span class="tag">{_html.escape(ch)}</span>' for ch in actions)
        if actions
        else "\u2014"
    )
    # New events from SSE have no feedback yet
    feedback_html = '<span class="muted">\u2014</span>'
    return (
        f'<tr id="event-live" class="event-unreviewed">'
        f"<td>{display_ts}</td>"
        f'<td>{_html.escape(event.get("class_name", "").replace("_", " ").title())}</td>'
        f"<td>{conf:.0%}</td>"
        f'<td>{_html.escape(event.get("camera_name", ""))}</td>'
        f'<td class="actions-cell">{actions_html}</td>'
        f"<td>{snap_html}</td>"
        f'<td class="feedback-cell">{feedback_html}</td>'
        f"</tr>"
    )


@router.get("/visits", response_class=HTMLResponse)
async def visits_page(
    request: Request,
    page: int = 1,
    camera: str = "",
    class_name: str = "",
    date_from: str = "",
    date_to: str = "",
) -> HTMLResponse:
    offset = (page - 1) * PAGE_SIZE
    cam = camera or None
    cls = class_name or None
    dfrom = date_from or None
    dto = date_to or None
    rows = db.get_visits(
        limit=PAGE_SIZE, offset=offset, camera=cam,
        class_name=cls, date_from=dfrom, date_to=dto,
    )
    total = db.count_visits(camera=cam, class_name=cls, date_from=dfrom, date_to=dto)
    tz = _tz_name()
    visits: list[dict] = []
    for row in rows:
        v = dict(row)
        v["display_start"] = _to_local(v.get("start_time", ""), tz)
        v["display_end"] = _to_local(v.get("end_time", ""), tz)
        # Format duration
        secs = v.get("duration_secs", 0)
        if secs >= 3600:
            v["display_duration"] = f"{secs / 3600:.1f}h"
        elif secs >= 60:
            v["display_duration"] = f"{secs / 60:.1f}m"
        else:
            v["display_duration"] = f"{secs:.0f}s"
        visits.append(v)
    return templates.TemplateResponse(
        request,
        "visits.html",
        {
            "visits": visits,
            "page": page,
            "total_pages": max(1, -(-total // PAGE_SIZE)),
            "total": total,
            "camera_names": _get_camera_names(),
            "filter_camera": camera,
            "filter_class": class_name,
            "filter_date_from": date_from,
            "filter_date_to": date_to,
        },
    )
