"""On-demand camera snapshot endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import config_store
import redis.asyncio as aioredis
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

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

    client = aioredis.Redis(host=host, port=port, decode_responses=True)
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
