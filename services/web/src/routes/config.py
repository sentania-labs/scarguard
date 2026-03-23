import json
import logging
from pathlib import Path

import config_store
import yaml
from config_model import (
    CameraConfig,
    DetectionConfig,
    NotificationsConfig,
    StructuredConfigPayload,
    SystemConfig,
)
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/config")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _parse_cfg(raw_cfg: dict) -> StructuredConfigPayload:
    """Parse config sections independently; each bad section falls back to its own default.

    Using per-section fallback means a single invalid field (e.g. a camera with an
    empty name) does not wipe out all other sections and cause the whole form to
    display default/empty values.
    """

    def _section(model_cls, data):
        try:
            return model_cls.model_validate(data if isinstance(data, dict) else {})
        except Exception as exc:
            log.warning(
                "Config section %s failed validation, using defaults: %s",
                model_cls.__name__,
                exc,
            )
            return model_cls()

    if not isinstance(raw_cfg, dict):
        return StructuredConfigPayload()

    cameras: list[CameraConfig] = []
    for i, cam in enumerate(raw_cfg.get("cameras", [])):
        try:
            cameras.append(CameraConfig.model_validate(cam))
        except Exception as exc:
            log.warning("Camera %d failed validation, skipping in form: %s", i, exc)

    return StructuredConfigPayload(
        system=_section(SystemConfig, raw_cfg.get("system", {})),
        cameras=cameras,
        detection=_section(DetectionConfig, raw_cfg.get("detection", {})),
        notifications=_section(NotificationsConfig, raw_cfg.get("notifications", {})),
    )


@router.get("", response_class=HTMLResponse)
async def config_page(request: Request):
    raw_cfg = config_store.load()
    raw = yaml.dump(raw_cfg, default_flow_style=False, sort_keys=False)

    # Build a validated config object with defaults for any missing fields.
    # Each section falls back independently so a single bad value does not
    # blank out the entire form.
    cfg = _parse_cfg(raw_cfg)

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

    For cameras, unknown fields (e.g. exclusion_zones) are preserved by merging
    the form values over the existing entry matched by name.
    """
    try:
        body = await request.json()
        payload = StructuredConfigPayload.model_validate(body)
    except ValidationError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Invalid request: {exc}"}, status_code=400)

    existing = config_store.load()

    existing["system"] = payload.system.model_dump()

    # Merge cameras: start from the existing entry (preserves exclusion_zones and
    # any other fields the form doesn't know about), then overlay the form values.
    existing_cameras_by_name: dict[str, dict] = {
        c["name"]: c
        for c in existing.get("cameras", [])
        if isinstance(c, dict) and "name" in c
    }
    merged_cameras = []
    for cam in payload.cameras:
        cam_dict = cam.model_dump()
        if cam.name in existing_cameras_by_name:
            merged = {**existing_cameras_by_name[cam.name], **cam_dict}
        else:
            merged = cam_dict
        merged_cameras.append(merged)
    existing["cameras"] = merged_cameras

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

    cfg = _parse_cfg(raw_cfg)

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
