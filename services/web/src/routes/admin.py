"""Admin routes — service log viewer and config backup management."""

import logging
import os
from pathlib import Path

import config_store
import redis.asyncio as aioredis
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

log = logging.getLogger(__name__)


def _require_admin(request: Request) -> bool:
    """Return True if the current user is an admin."""
    user = getattr(request.state, "user", None)
    return bool(user and user.get("is_admin"))

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

SERVICES = ["detector", "notifier", "web", "caddy"]

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
async def logs_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "logs.html", {"services": SERVICES}
    )


@router.get("/logs/stream")
async def logs_stream(
    request: Request,
    service: str = "detector",
    tail: int = 500,
) -> StreamingResponse:
    """SSE endpoint — streams log lines from the log-streamer sidecar via Redis."""
    if service not in SERVICES:
        service = "detector"
    tail = max(1, min(tail, 5000))

    async def generator():
        client = aioredis.Redis(**_redis_params())
        try:
            # Backfill: read last N lines from the ring buffer.
            # The buffer is newest-first (LPUSH order); reverse for chronological.
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

            # Live stream: subscribe to the pub/sub channel.
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
    """List all config backups."""
    if not _require_admin(request):
        return RedirectResponse("/", status_code=302)
    from main import backup_manager

    backups = backup_manager.list_backups() if backup_manager else []
    return templates.TemplateResponse(request, "backups.html", {"backups": backups})


@router.get("/backups/{name}/diff")
async def backup_diff(request: Request, name: str) -> JSONResponse:
    """Return a unified diff between a backup and the current config."""
    if not _require_admin(request):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    from main import backup_manager

    if not backup_manager:
        return JSONResponse(
            {"error": "Backup manager not initialized"}, status_code=500
        )
    if not name.startswith("scarguard_") or not name.endswith(".yml"):
        return JSONResponse({"error": "Invalid backup name"}, status_code=400)
    diff = backup_manager.get_diff(name)
    if diff is None:
        return JSONResponse({"error": "Backup not found"}, status_code=404)
    return JSONResponse({"diff": diff})


@router.post("/backups/{name}/restore")
async def backup_restore(request: Request, name: str) -> JSONResponse:
    """Restore a backup to the active config."""
    if not _require_admin(request):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
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
async def backup_create(request: Request) -> JSONResponse:
    """Create a manual config backup."""
    if not _require_admin(request):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    from main import backup_manager

    if not backup_manager:
        return JSONResponse(
            {"error": "Backup manager not initialized"}, status_code=500
        )
    filename = backup_manager.create_backup("manual")
    if not filename:
        return JSONResponse({"error": "Failed to create backup"}, status_code=500)
    return JSONResponse({"ok": True, "filename": filename})
