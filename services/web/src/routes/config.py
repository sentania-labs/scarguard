import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import available_timezones

import config_store
import db
import redis.asyncio as aioredis
import yaml
from config_model import (
    CameraConfig,
    DetectionConfig,
    NotificationsConfig,
    StructuredConfigPayload,
    SystemConfig,
    TLSConfig,
)
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/config")

_MODELS_DIR = Path(os.getenv("MODELS_DIR", "/models"))
_MODEL_EXTENSIONS = {".pt", ".engine", ".onnx"}
_TIMEZONES: list[str] = sorted(available_timezones())


def _list_models() -> list[str]:
    """Return sorted list of model paths available in MODELS_DIR."""
    try:
        return sorted(
            str(_MODELS_DIR / f.name)
            for f in _MODELS_DIR.iterdir()
            if f.is_file() and f.suffix in _MODEL_EXTENSIONS
        )
    except OSError:
        return []
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
        tls=_section(TLSConfig, raw_cfg.get("tls", {})),
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
    if not isinstance(raw_cfg, dict):
        return json.dumps([])
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
            "timezones": _TIMEZONES,
            "available_models": _list_models(),
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
    for nested_key in ("schedule", "auth", "summary_report", "backup"):
        if nested_key in system_dump:
            existing_nested = existing_system.get(nested_key, {})
            if not isinstance(existing_nested, dict):
                existing_nested = {}
            system_dump[nested_key] = {**existing_nested, **system_dump[nested_key]}
    existing["system"] = {**existing_system, **system_dump}
    # base_url removed in v0.12.4 — derived from tls.domain at runtime
    existing["system"].pop("base_url", None)

    # Merge cameras: start from the existing entry (preserves exclusion_zones and
    # any other fields the form doesn't know about), then overlay the form values.
    existing_cameras_by_name: dict[str, dict] = {
        c["name"]: c
        for c in existing.get("cameras", [])
        if isinstance(c, dict) and "name" in c
    }
    merged_cameras = []
    for cam in payload.cameras:
        cam_dict = cam.model_dump(exclude_unset=True)
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

    # TLS — detect changes so we can tell the UI that Caddy will reload.
    def _normalize_tls(raw: dict) -> dict:
        return {
            "mode": raw.get("mode", "off"),
            "domain": raw.get("domain", ""),
            "cert_path": raw.get("cert_path", "/config/certs/cert.pem"),
            "key_path": raw.get("key_path", "/config/certs/key.pem"),
        }

    tls_changed = _normalize_tls(existing.get("tls", {})) != _normalize_tls(
        payload.tls.model_dump()
    )
    existing["tls"] = payload.tls.model_dump()

    config_store.save(existing)
    return JSONResponse({"ok": True, "tls_changed": tls_changed})


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
            "timezones": _TIMEZONES,
            "available_models": _list_models(),
        },
    )


_CERTS_DIR = Path("/config/certs")
_MAX_CERT_SIZE = 64 * 1024  # 64 KB — generous for PEM bundles


@router.post("/tls/upload-cert", response_class=JSONResponse)
async def upload_tls_cert(
    request: Request,
    cert_file: UploadFile | None = File(None),
    key_file: UploadFile | None = File(None),
    cert_pem: str = Form(""),
    key_pem: str = Form(""),
) -> JSONResponse:
    """Upload or paste TLS certificate and key files.

    Accepts either file uploads (cert_file, key_file) or pasted PEM text
    (cert_pem, key_pem).  Files are written to /config/certs/ which is
    mounted from the scarguard-config volume.
    """
    _CERTS_DIR.mkdir(parents=True, exist_ok=True)

    cert_data: bytes | None = None
    key_data: bytes | None = None

    # Prefer file upload over pasted text
    if cert_file and cert_file.filename:
        cert_data = await cert_file.read()
    elif cert_pem.strip():
        cert_data = cert_pem.strip().encode()

    if key_file and key_file.filename:
        key_data = await key_file.read()
    elif key_pem.strip():
        key_data = key_pem.strip().encode()

    if not cert_data and not key_data:
        return JSONResponse(
            {"ok": False, "error": "No certificate or key provided"},
            status_code=400,
        )

    # Validate all files before writing any to avoid partial-write on error
    errors: list[str] = []
    to_write: list[tuple[str, bytes]] = []

    if cert_data:
        if len(cert_data) > _MAX_CERT_SIZE:
            errors.append("Certificate file too large (max 64 KB)")
        elif b"-----BEGIN" not in cert_data:
            errors.append("Certificate does not appear to be PEM-encoded")
        else:
            to_write.append(("cert.pem", cert_data))

    if key_data:
        if len(key_data) > _MAX_CERT_SIZE:
            errors.append("Key file too large (max 64 KB)")
        elif b"-----BEGIN" not in key_data:
            errors.append("Key does not appear to be PEM-encoded")
        else:
            to_write.append(("key.pem", key_data))

    if errors:
        return JSONResponse({"ok": False, "error": "; ".join(errors)}, status_code=400)

    # All validation passed — write files
    written: list[str] = []
    for name, data in to_write:
        path = _CERTS_DIR / name
        path.write_bytes(data)
        if name == "key.pem":
            path.chmod(0o600)
        written.append(name)

    log.info("TLS cert files uploaded: %s", ", ".join(written))
    return JSONResponse({"ok": True, "written": written})


# ── Test notification ─────────────────────────────────────────────────────────

_SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", "/data/snapshots"))
_TEST_IMAGE = Path(__file__).resolve().parent.parent / "static" / "test-fish.png"
_DETECTIONS_CHANNEL = "scarguard:detections"


@router.post("/test-notification", response_class=JSONResponse)
async def send_test_notification(request: Request) -> JSONResponse:
    """Send a test notification to a named channel via the notifier."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    channel: str = body.get("channel", "").strip()
    if not channel:
        return JSONResponse(
            {"ok": False, "error": "Channel name is required"},
            status_code=400,
        )

    cfg = config_store.load_cached()

    # Validate channel name against configured named channels
    raw_channels = cfg.get("notifications", {}).get("channels", [])
    valid_names = {
        ch["name"] for ch in raw_channels
        if isinstance(ch, dict) and ch.get("name")
    }
    if channel not in valid_names:
        return JSONResponse(
            {"ok": False, "error": f"Unknown channel: {channel}. Save config first."},
            status_code=400,
        )

    # Copy the bundled test image into the shared snapshot directory so the
    # notifier container can read it.
    snapshot_path: str | None = None
    if _TEST_IMAGE.is_file():
        dest = _SNAPSHOT_DIR / "test-fish.png"
        shutil.copy2(_TEST_IMAGE, dest)
        snapshot_path = str(dest)

    redis_cfg = cfg.get("redis", {})
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))

    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "class_name": "test_notification",
        "confidence": 1.0,
        "camera_name": "test",
        "snapshot_path": snapshot_path,
        "actions_triggered": [channel],
    }

    client = aioredis.Redis(host=host, port=port, password=os.environ.get("REDIS_PASSWORD", "") or None, decode_responses=True)
    try:
        await client.publish(_DETECTIONS_CHANNEL, json.dumps(event))
    finally:
        await client.close()

    log.info("Test notification sent to channel %s", channel)
    return JSONResponse({"ok": True})
