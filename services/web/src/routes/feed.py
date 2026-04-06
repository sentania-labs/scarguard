"""Live feed page — SSE stream pushes latest annotated snapshot on each detection."""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config_store
import db
import redis.asyncio as aioredis
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feed")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

CHANNEL = "scarguard:detections"

# Max buffered events per SSE client before dropping oldest.
_FEED_QUEUE_MAX = 32


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
    """SSE stream with per-client backpressure via bounded queue.

    A background task reads from Redis pub/sub into a bounded asyncio.Queue.
    If a slow client can't keep up, the oldest events are dropped so the
    server is never blocked.
    """
    redis_cfg = config_store.load_cached().get("redis", {})
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))

    async def generator():
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=_FEED_QUEUE_MAX)
        client = aioredis.Redis(host=host, port=port, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe(CHANNEL)

        async def _reader() -> None:
            """Read from Redis pubsub and enqueue; drop oldest on overflow."""
            try:
                while True:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=15.0,
                    )
                    if message is None:
                        await _put_or_drop(queue, None)
                        continue
                    if message["type"] != "message":
                        continue
                    await _put_or_drop(queue, message["data"])
            except asyncio.CancelledError:
                return
            except Exception:
                logger.debug("Feed reader error", exc_info=True)

        reader_task = asyncio.create_task(_reader())
        yield ": connected\n\n"
        try:
            while not await request.is_disconnected():
                try:
                    raw = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue

                if raw is None:
                    yield ": keepalive\n\n"
                    continue

                try:
                    event = json.loads(raw)
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
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
            await pubsub.unsubscribe(CHANNEL)
            await client.aclose()

    return StreamingResponse(generator(), media_type="text/event-stream")


async def _put_or_drop(queue: asyncio.Queue[str | None], item: str | None) -> None:
    """Put item into queue; if full, drop oldest first."""
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    await queue.put(item)
