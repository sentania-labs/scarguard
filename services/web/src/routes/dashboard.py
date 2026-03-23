from pathlib import Path

import config_store
import db
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    cfg = config_store.load()
    latest = db.get_latest_event()
    total = db.count_events()
    cameras = cfg.get("cameras", [])
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "armed": cfg.get("system", {}).get("armed", True),
            "cameras": cameras,
            "total_events": total,
            "latest": dict(latest) if latest else None,
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
