import json
from pathlib import Path

import config_store
import yaml
from config_model import StructuredConfigPayload
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

router = APIRouter(prefix="/config")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
async def config_page(request: Request):
    raw_cfg = config_store.load()
    raw = yaml.dump(raw_cfg, default_flow_style=False, sort_keys=False)

    # Build a validated config object with defaults for any missing fields.
    # model_validate is lenient — extra/unknown keys are ignored.
    try:
        cfg = StructuredConfigPayload.model_validate(raw_cfg)
    except Exception:
        # If config is badly malformed fall back to defaults so the form still renders.
        cfg = StructuredConfigPayload()

    cameras_json = json.dumps([c.model_dump() for c in cfg.cameras])

    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "raw_yaml": raw,
            "saved": False,
            "error": None,
            "cfg": cfg,
            "cameras_json": cameras_json,
        },
    )


@router.post("/structured", response_class=JSONResponse)
async def save_structured_config(request: Request) -> JSONResponse:
    """Accept JSON from the form-based config editor and write to scarguard.yml.

    Only updates the sections the form knows about (system, cameras, detection,
    notifications.discord, notifications.email).  All other keys in the existing
    config (redis, action_rules, webhooks, etc.) are preserved unchanged.
    """
    try:
        body = await request.json()
        payload = StructuredConfigPayload(**body)
    except ValidationError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Invalid request: {exc}"}, status_code=400)

    existing = config_store.load()

    existing["system"] = payload.system.model_dump()
    existing["cameras"] = [c.model_dump() for c in payload.cameras]
    existing["detection"] = payload.detection.model_dump()

    # Merge notifications: preserve webhooks and other channels not in the form.
    existing.setdefault("notifications", {})
    existing["notifications"]["discord"] = payload.notifications.discord.model_dump()
    existing["notifications"]["email"] = payload.notifications.email.model_dump()

    config_store.save(existing)
    return JSONResponse({"ok": True})


@router.post("", response_class=HTMLResponse)
async def save_config(request: Request, raw_yaml: str = Form(...)):
    error = None
    saved = False
    raw_cfg: dict = {}
    try:
        raw_cfg = yaml.safe_load(raw_yaml)
        if not isinstance(raw_cfg, dict):
            raise ValueError("Config must be a YAML mapping")
        config_store.save(raw_cfg)
        saved = True
        raw_yaml = yaml.dump(raw_cfg, default_flow_style=False, sort_keys=False)
    except Exception as exc:
        error = str(exc)

    try:
        cfg = StructuredConfigPayload.model_validate(raw_cfg)
    except Exception:
        cfg = StructuredConfigPayload()

    cameras_json = json.dumps([c.model_dump() for c in cfg.cameras])

    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "raw_yaml": raw_yaml,
            "saved": saved,
            "error": error,
            "cfg": cfg,
            "cameras_json": cameras_json,
        },
    )
