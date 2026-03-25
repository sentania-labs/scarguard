import json
import logging
from pathlib import Path

import config_store
import db
import yaml
from config_model import (
    CameraConfig,
    DetectionConfig,
    NotificationsConfig,
    SSLConfig,
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

    raw_cameras = raw_cfg.get("cameras", [])
    if not isinstance(raw_cameras, list):
        log.warning(
            "Config section cameras must be a list; got %s. Using empty list.",
            type(raw_cameras).__name__,
        )
        raw_cameras = []

    cameras: list[CameraConfig] = []
    for i, cam in enumerate(raw_cameras):
        try:
            cameras.append(CameraConfig.model_validate(cam))
        except Exception as exc:
            log.warning("Camera %d failed validation, skipping in form: %s", i, exc)

    return StructuredConfigPayload(
        system=_section(SystemConfig, raw_cfg.get("system", {})),
        cameras=cameras,
        detection=_section(DetectionConfig, raw_cfg.get("detection", {})),
        notifications=_section(NotificationsConfig, raw_cfg.get("notifications", {})),
        ssl=_section(SSLConfig, raw_cfg.get("ssl", {})),
    )


def _cameras_json(cfg_cameras: list[CameraConfig]) -> str:
    """Serialize cameras to JSON, including latest snapshot URL per camera."""
    latest_snaps = db.get_latest_snapshots_by_camera()
    result = []
    for cam in cfg_cameras:
        d = cam.model_dump()
        snap_path = latest_snaps.get(cam.name)
        d["snapshot_url"] = ("/snapshots/" + Path(snap_path).name) if snap_path else None
        result.append(d)
    return json.dumps(result)


def _channels_json(raw_cfg: dict) -> str:
    raw_notif = raw_cfg.get("notifications", {})
    channels = raw_notif.get("channels", []) if isinstance(raw_notif, dict) else []
    return json.dumps(channels if isinstance(channels, list) else [])


@router.get("", response_class=HTMLResponse)
async def config_page(request: Request):
    raw_cfg = config_store.load()
    raw = yaml.dump(raw_cfg, default_flow_style=False, sort_keys=False)

    # Build a validated config object with defaults for any missing fields.
    # Each section falls back independently so a single bad value does not
    # blank out the entire form.
    cfg = _parse_cfg(raw_cfg)

    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "raw_yaml": raw,
            "saved": False,
            "error": None,
            "cfg": cfg,
            "cameras_json": _cameras_json(cfg.cameras),
            "channels_json": _channels_json(raw_cfg),
        },
    )


@router.get("/raw")
async def get_raw_config() -> JSONResponse:
    """Return the current config as a raw YAML string for the Advanced editor."""
    cfg = config_store.load()
    raw = yaml.dump(cfg, default_flow_style=False, sort_keys=False)
    return JSONResponse({"yaml": raw})


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

    # Merge system settings so omitted structured-form fields are preserved.
    # The nested schedule dict is handled specially: merge it too so partial
    # schedule edits don't wipe unset fields.
    existing_system = existing.get("system", {})
    if not isinstance(existing_system, dict):
        existing_system = {}
    system_dump = payload.system.model_dump(exclude_unset=True)
    if "schedule" in system_dump:
        existing_schedule = existing_system.get("schedule", {})
        if not isinstance(existing_schedule, dict):
            existing_schedule = {}
        system_dump["schedule"] = {**existing_schedule, **system_dump["schedule"]}
    existing["system"] = {**existing_system, **system_dump}

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

    # Merge notifications: write legacy discord/email; always write channels list
    # (an empty list from the form intentionally clears all named channels).
    existing.setdefault("notifications", {})
    existing["notifications"]["discord"] = payload.notifications.discord.model_dump()
    existing["notifications"]["email"] = payload.notifications.email.model_dump()
    existing["notifications"]["channels"] = payload.notifications.channels

    # SSL — detect changes so we can tell the UI a restart is needed.
    # Normalize both sides through defaults so a missing ssl section in the
    # existing config doesn't false-positive as "changed" on every save.
    def _normalize(raw: dict) -> dict:
        return {
            "enabled": bool(raw.get("enabled", False)),
            "cert_path": raw.get("cert_path", "/certs/cert.pem"),
            "key_path": raw.get("key_path", "/certs/key.pem"),
            "https_only": bool(raw.get("https_only", False)),
            "keyfile_password": raw.get("keyfile_password", ""),
        }

    ssl_changed = _normalize(existing.get("ssl", {})) != _normalize(
        payload.ssl.model_dump()
    )
    new_ssl = payload.ssl.model_dump()
    # Strip empty keyfile_password so it doesn't clutter the YAML.
    if not new_ssl.get("keyfile_password"):
        new_ssl.pop("keyfile_password", None)
    existing["ssl"] = new_ssl

    config_store.save(existing)
    return JSONResponse({"ok": True, "ssl_changed": ssl_changed})


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

    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "raw_yaml": raw_yaml,
            "saved": saved,
            "error": error,
            "cfg": cfg,
            "cameras_json": _cameras_json(cfg.cameras),
            "channels_json": _channels_json(raw_cfg),
        },
    )
