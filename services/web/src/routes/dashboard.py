from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config_store
import db
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _to_local(iso_str: str, tz_name: str) -> str:
    """Convert a UTC ISO 8601 string to a formatted local-time string."""
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        tz = ZoneInfo("UTC")
    try:
        dt = datetime.fromisoformat(iso_str).astimezone(tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, TypeError):
        return str(iso_str)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    cfg = config_store.load()
    latest = db.get_latest_event()
    total = db.count_events()
    cameras = cfg.get("cameras", [])
    tz_name = cfg.get("system", {}).get("timezone", "UTC")
    latest_dict = None
    if latest:
        latest_dict = dict(latest)
        latest_dict["display_timestamp"] = _to_local(
            latest_dict.get("timestamp", ""), tz_name
        )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "armed": cfg.get("system", {}).get("armed", True),
            "cameras": cameras,
            "total_events": total,
            "latest": latest_dict,
            "model_path": cfg.get("detection", {}).get("model_path", "—"),
        },
    )


@router.post("/arm", response_class=HTMLResponse)
async def arm(request: Request):
    config_store.set_armed(True)
    return _arm_badge(request, armed=True)


@router.post("/disarm", response_class=HTMLResponse)
async def disarm(request: Request):
    config_store.set_armed(False)
    return _arm_badge(request, armed=False)


def _arm_badge(request: Request, *, armed: bool) -> HTMLResponse:
    """Return just the status badge fragment for HTMX swap."""
    return templates.TemplateResponse(
        request,
        "partials/arm_badge.html",
        {"armed": armed},
    )
