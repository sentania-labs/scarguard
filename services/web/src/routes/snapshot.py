"""On-demand camera snapshot endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import config_store
import redis.asyncio as aioredis
from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse
from route_auth import require_user
from starlette.responses import Response

logger = logging.getLogger(__name__)

router = APIRouter()

REQUEST_CHANNEL = "scarguard:snapshot:request"


@router.get("/snapshot/{camera_name}")
async def grab_snapshot(request: Request, camera_name: str) -> JSONResponse:
    """Request a live snapshot from a camera via the detector service."""
    cfg = config_store.load_cached()
    redis_cfg = cfg.get("redis", {})
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))

    request_id = uuid.uuid4().hex
    result_channel = f"scarguard:snapshot:result:{request_id}"

    client = aioredis.Redis(host=host, port=port, password=os.environ.get("REDIS_PASSWORD", "") or None, decode_responses=True)
    try:
        # Subscribe to result channel before publishing request
        pubsub = client.pubsub()
        await pubsub.subscribe(result_channel)

        # Publish request
        await client.publish(
            REQUEST_CHANNEL,
            json.dumps({"camera_name": camera_name, "request_id": request_id}),
        )

        # Wait for response with timeout
        deadline = 10.0  # seconds
        elapsed = 0.0
        while elapsed < deadline:
            msg = await pubsub.get_message(timeout=1.0)
            if msg and msg["type"] == "message":
                try:
                    result = json.loads(msg["data"])
                except (json.JSONDecodeError, TypeError):
                    continue

                if result.get("ok"):
                    filename = result.get("filename", "")
                    return JSONResponse({
                        "ok": True,
                        "snapshot_url": f"/snapshots/{filename}",
                        "filename": filename,
                    })
                else:
                    return JSONResponse(
                        {"ok": False, "error": result.get("error", "Unknown error")},
                        status_code=500,
                    )
            elapsed += 1.0
            await asyncio.sleep(0)  # yield to event loop

        return JSONResponse(
            {"ok": False, "error": "Snapshot request timed out"},
            status_code=504,
        )
    finally:
        await pubsub.unsubscribe(result_channel)
        await client.close()


DETECTIONS_CHANNEL = "scarguard:detections"
_SNAPSHOT_DIR = os.getenv("SNAPSHOT_DIR", "/data/snapshots")


@router.post("/snapshot/send", response_model=None)
async def send_snapshot_to_channel(
    request: Request,
    filename: str = Form(...),
    channel: str = Form(...),
    camera_name: str = Form(""),
) -> JSONResponse | Response:
    """Send an existing snapshot to a notification channel via the notifier."""
    gate = require_user(request, is_api=True)
    if not isinstance(gate, dict):
        return gate
    # Validate the snapshot file exists
    snapshot_path = os.path.join(_SNAPSHOT_DIR, os.path.basename(filename))
    if not os.path.isfile(snapshot_path):
        return JSONResponse(
            {"ok": False, "error": "Snapshot file not found"},
            status_code=404,
        )

    cfg = config_store.load_cached()

    # Validate channel name against configured channels
    raw_channels = cfg.get("notifications", {}).get("channels", [])
    valid_names = {
        ch["name"] for ch in raw_channels
        if isinstance(ch, dict) and ch.get("name")
    }
    if channel not in valid_names:
        return JSONResponse(
            {"ok": False, "error": f"Unknown channel: {channel}"},
            status_code=400,
        )
    redis_cfg = cfg.get("redis", {})
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))

    # Build a synthetic event that the notifier will dispatch to the named channel
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "class_name": "snapshot_share",
        "confidence": 1.0,
        "camera_name": camera_name or "manual",
        "snapshot_path": snapshot_path,
        "actions_triggered": [channel],
    }

    client = aioredis.Redis(host=host, port=port, password=os.environ.get("REDIS_PASSWORD", "") or None, decode_responses=True)
    try:
        await client.publish(DETECTIONS_CHANNEL, json.dumps(event))
    finally:
        await client.close()

    logger.info("Snapshot %s sent to channel %s", filename, channel)
    return JSONResponse({"ok": True})
