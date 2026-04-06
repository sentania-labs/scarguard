"""About page — project info, version, and component status."""

import logging
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

import config_store
import db
import redis as redis_lib
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# Build-time metadata injected via Docker build args (see Dockerfile).
VERSION: str = os.environ.get("VERSION", "local-dev")
GIT_COMMIT: str = os.environ.get("GIT_COMMIT", "unknown")
BUILD_DATE: str = os.environ.get("BUILD_DATE", "unknown")


def _redis_conn(cfg: dict) -> redis_lib.Redis:
    redis_cfg = cfg.get("redis", {})
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))
    pw = os.environ.get("REDIS_PASSWORD", "") or None
    return redis_lib.Redis(
        host=host, port=port, password=pw,
        socket_timeout=2, socket_connect_timeout=2,
    )


def _check_redis(cfg: dict) -> bool:
    try:
        r = _redis_conn(cfg)
        r.ping()
        return True
    except Exception:
        return False


def _check_log_streamer(cfg: dict) -> bool:
    """Return True if the log-streamer sidecar has populated any ring buffers."""
    try:
        r = _redis_conn(cfg)
        keys: list = list(r.keys("scarguard:logs:buffer:*"))
        return len(keys) > 0
    except Exception:
        return False


def _time_ago(timestamp_str: str) -> str:
    """Return a human-readable 'X ago' string from an ISO timestamp."""
    try:
        ts = datetime.fromisoformat(timestamp_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(tz=timezone.utc) - ts
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"
    except Exception:
        return timestamp_str


@router.get("/about", response_class=HTMLResponse)
async def about_page(request: Request) -> HTMLResponse:
    cfg = config_store.load()

    # Component status
    redis_ok = _check_redis(cfg)
    log_streamer_ok = _check_log_streamer(cfg) if redis_ok else False

    latest_event = None
    latest_ago: str | None = None
    today_count = 0
    try:
        latest_event = db.get_latest_event()
        if latest_event:
            latest_ago = _time_ago(latest_event["timestamp"])
        today_count = db.count_events_today()
    except Exception:
        log.warning("Could not read detection events for about page", exc_info=True)

    return templates.TemplateResponse(
        request,
        "about.html",
        {
            "version": VERSION,
            "git_commit": GIT_COMMIT,
            "build_date": BUILD_DATE,
            "python_version": sys.version.split()[0],
            "platform": platform.machine(),
            "config_path": os.environ.get("CONFIG_PATH", "/config/scarguard.yml"),
            "model_path": cfg.get("detection", {}).get("model_path", "unknown"),
            "armed": cfg.get("system", {}).get("armed", False),
            "cameras": cfg.get("cameras", []),
            "redis_ok": redis_ok,
            "log_streamer_ok": log_streamer_ok,
            "latest_event": latest_event,
            "latest_ago": latest_ago,
            "today_count": today_count,
        },
    )
