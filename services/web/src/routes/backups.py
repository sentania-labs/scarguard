"""Database backups admin page — list / trigger / download SQLite backups.

Reads the on-disk inventory at ``/data/backups`` produced by the v1.14
backup sidecar, supports admin-triggered manual backups via Redis
pub/sub, and serves backup files for download. Filenames are validated
against the actual directory listing before serving — no path-traversal
input ever reaches the file system.

Distinct from ``/admin/backups`` which manages YAML-config snapshots
via ``ConfigBackupManager`` (a v0.9 feature). This route is at
``/admin/db-backups`` to avoid the name collision.
"""

from __future__ import annotations

import json
import logging
import os
import time as _time
import uuid
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from rate_limit_dep import rate_limit
from route_auth import require_admin
from starlette.responses import Response

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/db-backups")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

BACKUP_ROOT = Path(os.environ.get("BACKUP_ROOT", "/data/backups"))

TRIGGER_CHANNEL = "scarguard:backup:trigger"
STATUS_CHANNEL = "scarguard:backup:status"


def _list_backups() -> list[dict[str, Any]]:
    """Walk BACKUP_ROOT and return a flat list of every backup file.

    Each entry: ``{db, filename, size_bytes, mtime_iso, rel_path}``.
    Sorted newest-first."""
    out: list[dict[str, Any]] = []
    if not BACKUP_ROOT.exists():
        return out
    for db_dir in sorted(BACKUP_ROOT.iterdir()):
        if not db_dir.is_dir():
            continue
        for f in sorted(db_dir.glob("*.db*"), reverse=True):
            try:
                stat = f.stat()
            except OSError:
                continue
            from datetime import datetime, timezone
            out.append({
                "db": db_dir.name,
                "filename": f.name,
                "rel_path": f"{db_dir.name}/{f.name}",
                "size_bytes": stat.st_size,
                "size_human": _human_size(stat.st_size),
                "mtime_iso": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc,
                ).isoformat(),
            })
    out.sort(key=lambda e: e["mtime_iso"], reverse=True)
    return out


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n = n / 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


def _safe_resolve(rel_path: str) -> Path | None:
    """Resolve ``{db}/{filename}`` against BACKUP_ROOT and verify the
    resolved path stays inside it. Returns None if invalid."""
    if not rel_path or ".." in rel_path or rel_path.startswith("/"):
        return None
    candidate = (BACKUP_ROOT / rel_path).resolve()
    try:
        candidate.relative_to(BACKUP_ROOT.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


@router.get("", response_class=HTMLResponse)
async def backups_page(request: Request) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    backups = _list_backups()
    return templates.TemplateResponse(
        request,
        "db_backups.html",
        {
            "backups": backups,
            "total_count": len(backups),
            "total_size_human": _human_size(sum(b["size_bytes"] for b in backups)),
            "backup_root": str(BACKUP_ROOT),
        },
    )


def _redis_params() -> dict[str, Any]:
    import config_store
    redis_cfg = config_store.load_cached().get("redis", {}) or {}
    return {
        "host": redis_cfg.get("host", "redis"),
        "port": int(redis_cfg.get("port", 6379)),
        "password": os.environ.get("REDIS_PASSWORD", "") or None,
        "decode_responses": True,
    }


@router.post(
    "/trigger", response_class=JSONResponse,
    dependencies=[Depends(rate_limit("backup-trigger", capacity=10, window_seconds=300))],
)
async def trigger_backup(request: Request) -> Response:
    """Publish a manual-trigger message to the backup sidecar."""
    gate = require_admin(request, is_api=True)
    if not isinstance(gate, dict):
        return gate

    request_id = uuid.uuid4().hex[:12]
    log.info("Backup manually triggered [rid=%s]", request_id)

    client = aioredis.Redis(**_redis_params())
    try:
        await client.publish(
            TRIGGER_CHANNEL, json.dumps({"request_id": request_id}),
        )
    except Exception:
        log.exception("Failed to publish backup trigger")
        return JSONResponse(
            {"ok": False, "error": "Backup trigger publish failed",
             "request_id": request_id},
            status_code=502,
        )
    finally:
        await client.close()

    return JSONResponse({
        "ok": True,
        "request_id": request_id,
        "note": "Backup started. Refresh in a few seconds to see the new file.",
    })


@router.get("/status", response_class=JSONResponse)
async def latest_status(request: Request) -> Response:
    """Return the most recent status message from the backup sidecar.

    Subscribes briefly to STATUS_CHANNEL and returns the first message
    received, or a synthetic "no recent activity" if nothing arrives.
    For full history the operator reads the docker logs."""
    gate = require_admin(request, is_api=True)
    if not isinstance(gate, dict):
        return gate

    client = aioredis.Redis(**_redis_params())
    try:
        pubsub = client.pubsub()
        await pubsub.subscribe(STATUS_CHANNEL)
        deadline = _time.monotonic() + 1.0
        while _time.monotonic() < deadline:
            msg = await pubsub.get_message(timeout=0.5)
            if msg and msg["type"] == "message":
                try:
                    return JSONResponse(json.loads(msg["data"]))
                except (json.JSONDecodeError, TypeError):
                    continue
        await pubsub.unsubscribe(STATUS_CHANNEL)
        return JSONResponse({"phase": "idle"})
    finally:
        await client.close()


@router.get("/download/{db}/{filename}")
async def download_backup(
    request: Request, db: str, filename: str,
) -> Response:
    """Stream a backup file to the admin browser.

    Filename and db come from the URL but are validated against the
    actual directory listing — anything else returns 404. This is
    audit-logged so manual exfiltration leaves a trail."""
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate

    rel = f"{db}/{filename}"
    resolved = _safe_resolve(rel)
    if resolved is None:
        log.warning(
            "Rejected backup download for %r (resolved=None)", rel,
        )
        raise HTTPException(status_code=404, detail="Backup not found")

    log.info(
        "Backup downloaded: %s by %s",
        rel,
        getattr(request.state, "user", {}).get("username", "<unknown>"),
    )
    media_type = "application/gzip" if filename.endswith(".gz") else "application/octet-stream"
    return FileResponse(
        path=str(resolved),
        media_type=media_type,
        filename=filename,
    )
