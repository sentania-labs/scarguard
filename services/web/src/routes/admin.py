"""Admin routes — service log viewer and config backup management."""

import logging
import os
from pathlib import Path

import config_store
import redis.asyncio as aioredis
from config_redact import redact_yaml
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from route_auth import has_admin_access, require_admin, require_viewer
from sse_limiter import SSETooManyStreams, sse_connection
from starlette.responses import Response

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

SERVICES = ["detector", "notifier", "deterrent", "web", "caddy"]

# Redis key prefixes — must match log-streamer sidecar constants.
_CHANNEL_PREFIX = "scarguard:logs:"
_BUFFER_PREFIX = "scarguard:logs:buffer:"


def _redis_params() -> dict:
    """Return Redis connection kwargs from config + environment."""
    cfg = config_store.load_cached()
    redis_cfg = cfg.get("redis", {})
    return {
        "host": redis_cfg.get("host", "redis"),
        "port": int(redis_cfg.get("port", 6379)),
        "password": os.environ.get("REDIS_PASSWORD", "") or None,
        "decode_responses": True,
    }


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request) -> Response:
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate
    return templates.TemplateResponse(
        request, "logs.html", {"services": SERVICES}
    )


@router.get("/logs/stream")
async def logs_stream(
    request: Request,
    service: str = "detector",
    tail: int = 500,
) -> Response:
    """SSE endpoint — streams log lines from the log-streamer sidecar via Redis."""
    gate = require_viewer(request, is_api=True)
    if not isinstance(gate, dict):
        return gate
    if service not in SERVICES:
        service = "detector"
    tail = max(1, min(tail, 5000))

    user = getattr(request.state, "user", None)
    user_id = user["user_id"] if user else "anon"

    async def generator():
        client = aioredis.Redis(**_redis_params())
        try:
            async with sse_connection(client, user_id):
                buffer_key = f"{_BUFFER_PREFIX}{service}"
                lines = await client.lrange(buffer_key, 0, tail - 1)
                if not lines:
                    yield (
                        "data: [ScarGuard] No log history available yet "
                        "— waiting for live lines from log-streamer sidecar...\n\n"
                    )
                else:
                    for line in reversed(lines):
                        safe = line.replace("\n", "  ")
                        yield f"data: {safe}\n\n"

                channel = f"{_CHANNEL_PREFIX}{service}"
                pubsub = client.pubsub()
                await pubsub.subscribe(channel)
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
                        safe = str(message["data"]).replace("\n", "  ")
                        yield f"data: {safe}\n\n"
                finally:
                    await pubsub.unsubscribe(channel)
        except SSETooManyStreams:
            yield "event: error\ndata: Too many active streams\n\n"
        finally:
            await client.aclose()

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Config Backup Management ─────────────────────────────────────────────────


@router.get("/backups", response_class=HTMLResponse)
async def backups_page(request: Request) -> Response:
    """List all config backups. Viewable by viewer and admin (list is non-secret)."""
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate
    from main import backup_manager

    backups = backup_manager.list_backups() if backup_manager else []
    return templates.TemplateResponse(request, "backups.html", {"backups": backups})


@router.get("/backups/{name}/diff")
async def backup_diff(request: Request, name: str) -> Response:
    """Return a unified diff between a backup and the current config.

    Viewers see the diff with sensitive lines collapsed to ``***REDACTED***``
    (we redact both sides of the comparison before diffing, so pure-secret
    edits render as no-change).  Admins see the full plaintext diff.
    """
    gate = require_viewer(request, is_api=True)
    if not isinstance(gate, dict):
        return gate
    from main import backup_manager

    if not backup_manager:
        return JSONResponse(
            {"error": "Backup manager not initialized"}, status_code=500
        )
    if not name.startswith("scarguard_") or not name.endswith(".yml"):
        return JSONResponse({"error": "Invalid backup name"}, status_code=400)
    # When the caller is a viewer (not admin), produce the diff from redacted
    # copies of both files so no plaintext secrets leak into the diff output.
    if has_admin_access(request):
        diff = backup_manager.get_diff(name)
    else:
        diff = backup_manager.get_diff(name, transform=redact_yaml)
    if diff is None:
        return JSONResponse({"error": "Backup not found"}, status_code=404)
    return JSONResponse({"diff": diff})


@router.post("/backups/{name}/restore")
async def backup_restore(request: Request, name: str) -> Response:
    """Restore a backup to the active config. Admin only."""
    gate = require_admin(request, is_api=True)
    if not isinstance(gate, dict):
        return gate
    from main import backup_manager

    if not backup_manager:
        return JSONResponse(
            {"error": "Backup manager not initialized"}, status_code=500
        )
    if not name.startswith("scarguard_") or not name.endswith(".yml"):
        return JSONResponse({"error": "Invalid backup name"}, status_code=400)
    ok = backup_manager.restore(name)
    if not ok:
        return JSONResponse({"error": "Restore failed"}, status_code=500)
    return JSONResponse({"ok": True, "message": f"Restored from {name}"})


@router.post("/backups/create")
async def backup_create(request: Request) -> Response:
    """Create a manual config backup. Admin only."""
    gate = require_admin(request, is_api=True)
    if not isinstance(gate, dict):
        return gate
    from main import backup_manager

    if not backup_manager:
        return JSONResponse(
            {"error": "Backup manager not initialized"}, status_code=500
        )
    filename = backup_manager.create_backup("manual")
    if not filename:
        return JSONResponse({"error": "Failed to create backup"}, status_code=500)
    return JSONResponse({"ok": True, "filename": filename})
