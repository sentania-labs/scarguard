import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import available_timezones

import audit
import config_store
import db
import redis.asyncio as aioredis
import yaml
from config_model import (
    ActuationConfig,
    CameraConfig,
    DetectionConfig,
    NotificationsConfig,
    StructuredConfigPayload,
    SystemConfig,
    TLSConfig,
)
from config_redact import (
    _CHANNEL_SENSITIVE_KEYS,
    _STRUCTURAL_PATHS,
    REDACTED_PLACEHOLDER,
    redact_config,
)
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from rate_limit_dep import rate_limit
from route_auth import has_admin_access, require_admin, require_viewer
from starlette.responses import Response
from template_json import safe_json_dumps

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
        deterrent=_section(ActuationConfig, raw_cfg.get("deterrent", {})),
    )


def _redact_parsed_cfg(cfg: StructuredConfigPayload) -> None:
    """Mask secrets on an already-parsed ``StructuredConfigPayload`` in place.

    Used by the viewer (read-only admin) branch of `config_page`.  Parsing
    happens first against the raw config (so structural validators pass),
    then this walker replaces the sensitive leaves with
    ``REDACTED_PLACEHOLDER``.  Keep this list in sync with the structural
    paths in `config_redact._STRUCTURAL_PATHS` — both walkers cover the
    same typed fields, just at different layers (dict vs Pydantic model).
    Per-channel masking for `notifications.channels` still happens via
    `redact_config` on the raw dict that feeds `_channels_json`.
    """
    for cam in cfg.cameras:
        if cam.rtsp_url:
            cam.rtsp_url = REDACTED_PLACEHOLDER
    tuya = cfg.deterrent.tuya
    if tuya.api_key:
        tuya.api_key = REDACTED_PLACEHOLDER
    if tuya.api_secret:
        tuya.api_secret = REDACTED_PLACEHOLDER


def _cameras_json(cfg_cameras: list[CameraConfig]) -> str:
    """Serialize cameras to JSON, including latest snapshot URL per camera."""
    latest_snaps = db.get_latest_snapshots_by_camera()
    result = []
    for cam in cfg_cameras:
        d = cam.model_dump()
        snap_path = latest_snaps.get(cam.name)
        d["snapshot_url"] = ("/snapshots/" + Path(snap_path).name) if snap_path else None
        result.append(d)
    return safe_json_dumps(result)


def _groups_json(raw_cfg: dict) -> str:
    """Extract deterrent group names for client-side chip-picker registry."""
    det = raw_cfg.get("deterrent") if isinstance(raw_cfg, dict) else None
    groups: list = []
    if isinstance(det, dict):
        raw_groups = det.get("groups", [])
        if isinstance(raw_groups, list):
            groups = [
                g.get("name", "") for g in raw_groups
                if isinstance(g, dict) and g.get("name")
            ]
    return safe_json_dumps(groups)


def _channels_json(raw_cfg: dict) -> str:
    if not isinstance(raw_cfg, dict):
        return safe_json_dumps([])
    raw_notif = raw_cfg.get("notifications", {})
    channels = raw_notif.get("channels", []) if isinstance(raw_notif, dict) else []
    return safe_json_dumps(channels if isinstance(channels, list) else [])


@router.get("", response_class=HTMLResponse)
async def config_page(request: Request) -> Response:
    """Render the config editor.

    Viewers get a read-only copy with sensitive fields replaced by
    ``***REDACTED***`` (camera RTSP URLs, Discord webhook URLs, SMTP /
    webhook / ntfy credentials).  The Advanced/raw-YAML tab is hidden
    for viewers entirely — see config.html for the template branches.
    """
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate

    raw_cfg = config_store.load()
    is_admin = has_admin_access(request)

    # Parse from the unredacted dict so structural validation (e.g.
    # `CameraConfig.rtsp_url` requiring an rtsp:// scheme) succeeds.  The
    # masked placeholder would fail that validator and cause every camera
    # with a real URL to be dropped from the form for viewers.  Secrets are
    # masked *after* parsing, in-place on the Pydantic objects, via
    # `_redact_parsed_cfg` below.
    cfg = _parse_cfg(raw_cfg)

    # Mask secrets for ALL roles (admin included) so cleartext secrets
    # never appear in rendered HTML.  Admins can reveal secrets on-demand
    # via the /config/secrets endpoint (audit-logged, fetch-only).
    # This closes the XSS amplification vector flagged by the red-team
    # audit: even if an attacker injects script, the DOM contains only
    # redacted placeholders — not cleartext credentials.
    _redact_parsed_cfg(cfg)
    raw_cfg_for_render = redact_config(raw_cfg)
    raw = yaml.dump(raw_cfg_for_render, default_flow_style=False, sort_keys=False)

    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "raw_yaml": raw,
            "saved": False,
            "error": None,
            "cfg": cfg,
            "cameras_json": _cameras_json(cfg.cameras),
            "channels_json": _channels_json(raw_cfg_for_render),
            "groups_json": _groups_json(raw_cfg_for_render),
            "timezones": _TIMEZONES,
            "available_models": _list_models(),
            "read_only": not is_admin,
        },
    )


@router.get("/raw")
async def get_raw_config(request: Request) -> Response:
    """Return the current config as a redacted YAML string for the Advanced editor.

    Admin-only.  Returns redacted YAML by default so cleartext secrets
    never travel to the browser.  The unredacted version is available via
    ``/config/raw-unredacted`` which logs an audit event.
    """
    gate = require_admin(request, is_api=True)
    if not isinstance(gate, dict):
        return gate
    cfg = config_store.load()
    redacted = redact_config(cfg)
    raw = yaml.dump(redacted, default_flow_style=False, sort_keys=False)
    return JSONResponse({"yaml": raw})


@router.get("/raw-unredacted")
async def get_raw_config_unredacted(request: Request) -> Response:
    """Return the unredacted raw YAML.  Admin-only, audit-logged.

    Used by the "Reveal secrets" button in the raw-YAML tab.  Every call
    is recorded in the audit log so there is a trail when secrets are
    viewed in cleartext.
    """
    gate = require_admin(request, is_api=True)
    if not isinstance(gate, dict):
        return gate
    audit.record_request(
        request,
        action="secret.reveal",
        resource="config/raw-unredacted",
    )
    cfg = config_store.load()
    raw = yaml.dump(cfg, default_flow_style=False, sort_keys=False)
    return JSONResponse({"yaml": raw})


@router.get("/secrets")
async def get_secrets(request: Request) -> Response:
    """Return unredacted secret field values as JSON.  Admin-only, audit-logged.

    Used by the "Reveal secrets" button in the structured-form tab.  The
    response includes only the sensitive fields so the client can patch
    ``***REDACTED***`` placeholders in form inputs without re-fetching the
    entire config.

    Every call is recorded in the audit log.
    """
    gate = require_admin(request, is_api=True)
    if not isinstance(gate, dict):
        return gate
    audit.record_request(
        request,
        action="secret.reveal",
        resource="config/secrets",
    )
    cfg = config_store.load()

    result: dict[str, Any] = {}

    # Structural paths — tuya keys
    for path in _STRUCTURAL_PATHS:
        val: Any = cfg
        for seg in path:
            if seg == "[]":
                break
            if isinstance(val, dict):
                val = val.get(seg)
            else:
                val = None
                break
        if isinstance(val, str) and val:
            result[path[-1]] = val

    # Camera RTSP URLs
    cameras: list[dict[str, str]] = []
    raw_cameras = cfg.get("cameras", [])
    if isinstance(raw_cameras, list):
        for cam in raw_cameras:
            if isinstance(cam, dict) and cam.get("rtsp_url"):
                cameras.append({
                    "name": cam.get("name", ""),
                    "rtsp_url": cam["rtsp_url"],
                })
    if cameras:
        result["cameras"] = cameras

    # Channel secrets
    channels: list[dict[str, Any]] = []
    raw_notif = cfg.get("notifications", {})
    raw_channels = raw_notif.get("channels", []) if isinstance(raw_notif, dict) else []
    if isinstance(raw_channels, list):
        for ch in raw_channels:
            if not isinstance(ch, dict):
                continue
            secrets: dict[str, Any] = {"name": ch.get("name", "")}
            has_secret = False
            for key in _CHANNEL_SENSITIVE_KEYS:
                if key in ch and ch[key]:
                    secrets[key] = ch[key]
                    has_secret = True
            if has_secret:
                channels.append(secrets)
    if channels:
        result["channels"] = channels

    return JSONResponse(result)


@router.post(
    "/structured", response_class=JSONResponse,
    dependencies=[Depends(rate_limit("config-save", capacity=20, window_seconds=60))],
)
async def save_structured_config(request: Request) -> Response:
    """Accept JSON from the form-based config editor and write to scarguard.yml.

    Admin only — viewers are blocked at the top of the handler.

    Only updates the sections the form knows about (system, cameras, detection,
    notifications.channels).  All other keys in the existing config (redis,
    webhooks, etc.) are preserved unchanged.

    For cameras, unknown fields (e.g. exclusion_zones) are preserved by merging
    the form values over the existing entry matched by name.
    """
    gate = require_admin(request, is_api=True)
    if not isinstance(gate, dict):
        return gate
    try:
        body = await request.json()
        payload = StructuredConfigPayload.model_validate(body)
    except ValidationError as exc:
        # Don't echo pydantic's error text to the client — log it server-side
        # under a short request ID the operator can grep for. CodeQL
        # py/stack-trace-exposure (issue #95).
        req_id = uuid.uuid4().hex[:8]
        log.warning("config save validation error [%s]: %s", req_id, exc)
        return JSONResponse(
            {"ok": False, "error": "Invalid config payload", "request_id": req_id},
            status_code=422,
        )
    except Exception:
        req_id = uuid.uuid4().hex[:8]
        log.exception("config save failed [%s]", req_id)
        return JSONResponse(
            {"ok": False, "error": "Unable to save config", "request_id": req_id},
            status_code=400,
        )

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
    # Drop REDACTED_PLACEHOLDER values so they don't overwrite real secrets —
    # the form ships redacted placeholders by default; only revealed values
    # should update the persisted config.
    existing_cameras_by_name: dict[str, dict] = {
        c["name"]: c
        for c in existing.get("cameras", [])
        if isinstance(c, dict) and "name" in c
    }
    merged_cameras = []
    for cam in payload.cameras:
        cam_dict = cam.model_dump(exclude_unset=True)
        # Strip redacted placeholders so the merge keeps the existing secret.
        cam_dict = {
            k: v for k, v in cam_dict.items()
            if v != REDACTED_PLACEHOLDER
        }
        if cam.name in existing_cameras_by_name:
            merged = {**existing_cameras_by_name[cam.name], **cam_dict}
        else:
            merged = cam_dict
        merged_cameras.append(merged)
    existing["cameras"] = merged_cameras

    existing["detection"] = payload.detection.model_dump()

    # Merge notifications: write channels list only (legacy discord/email keys
    # removed in v0.13.2 — stripped automatically by config_store on save).
    # Strip redacted placeholders from channel dicts so they don't overwrite
    # real secrets when secrets are not revealed.
    existing.setdefault("notifications", {})
    existing_channels_by_name: dict[str, dict] = {
        c["name"]: c
        for c in (existing.get("notifications", {}).get("channels") or [])
        if isinstance(c, dict) and "name" in c
    }
    merged_channels: list[Any] = []
    for ch in payload.notifications.channels:
        if not isinstance(ch, dict):
            merged_channels.append(ch)
            continue
        cleaned = {
            k: v for k, v in ch.items()
            if v != REDACTED_PLACEHOLDER
        }
        ch_name = cleaned.get("name", "")
        if ch_name and ch_name in existing_channels_by_name:
            merged_ch = {**existing_channels_by_name[ch_name], **cleaned}
        else:
            merged_ch = cleaned
        merged_channels.append(merged_ch)
    existing["notifications"]["channels"] = merged_channels

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

    # Merge deterrent: the config page only sends ``enabled``; the full
    # device list and credentials live on the dedicated /admin/deterrent page.
    # Only merge the fields the structured form actually controls to avoid
    # wiping Tuya keys/devices with empty defaults on unrelated config saves.
    existing_act = existing.get("deterrent", {})
    if not isinstance(existing_act, dict):
        existing_act = {}
    existing_act["enabled"] = payload.deterrent.enabled
    existing["deterrent"] = existing_act

    config_store.save(existing)
    warnings = _find_orphan_references(existing)
    audit.record_request(
        request,
        action="config.save",
        resource="scarguard.yml",
        details={
            "form": "structured",
            "tls_changed": tls_changed,
            "orphan_warnings": len(warnings),
        },
    )
    return JSONResponse({
        "ok": True,
        "tls_changed": tls_changed,
        "warnings": warnings,
    })


def _find_orphan_references(cfg: dict[str, Any]) -> list[str]:
    """Return human-readable warnings for rule/report references that don't
    resolve to a defined channel or deterrent group.

    The save still succeeds — this is advisory, per the "you edited the
    YAML, you know what's up" philosophy.  The warnings surface in the
    save response so the UI can remind the user until they fix it.
    """
    if not isinstance(cfg, dict):
        return []
    notif = cfg.get("notifications") or {}
    channel_names: set[str] = set()
    if isinstance(notif, dict):
        for ch in notif.get("channels", []) or []:
            if isinstance(ch, dict) and isinstance(ch.get("name"), str) and ch["name"]:
                channel_names.add(ch["name"])

    det = cfg.get("deterrent") or {}
    group_names: set[str] = set()
    if isinstance(det, dict):
        for g in det.get("groups", []) or []:
            if isinstance(g, dict) and isinstance(g.get("name"), str) and g["name"]:
                group_names.add(g["name"])

    warnings: list[str] = []
    for cam in cfg.get("cameras", []) or []:
        if not isinstance(cam, dict):
            continue
        cam_name = cam.get("name", "<unnamed camera>")
        if not isinstance(cam_name, str):
            cam_name = "<unnamed camera>"
        for rule in cam.get("notification_rules", []) or []:
            if not isinstance(rule, dict):
                continue
            for ch_ref in rule.get("channels", []) or []:
                if isinstance(ch_ref, str) and ch_ref and ch_ref not in channel_names:
                    warnings.append(
                        f"Camera '{cam_name}' notification rule references "
                        f"unknown channel '{ch_ref}'"
                    )
        for rule in cam.get("deterrent_rules", []) or []:
            if not isinstance(rule, dict):
                continue
            for g_ref in rule.get("groups", []) or []:
                if isinstance(g_ref, str) and g_ref and g_ref not in group_names:
                    warnings.append(
                        f"Camera '{cam_name}' deterrent rule references "
                        f"unknown group '{g_ref}'"
                    )

    sys_cfg = cfg.get("system") or {}
    if isinstance(sys_cfg, dict):
        sr = sys_cfg.get("summary_report") or {}
        if isinstance(sr, dict):
            for ch_ref in sr.get("channels", []) or []:
                if isinstance(ch_ref, str) and ch_ref and ch_ref not in channel_names:
                    warnings.append(
                        f"Summary report references unknown channel '{ch_ref}'"
                    )

    return warnings


_MAX_RAW_YAML_BYTES = 1_000_000  # 1 MB ceiling on raw-YAML uploads


@router.post(
    "", response_class=HTMLResponse,
    dependencies=[Depends(rate_limit("config-save", capacity=20, window_seconds=60))],
)
async def save_config(request: Request, raw_yaml: str = Form(...)) -> Response:
    """Save raw-YAML config. Admin only."""
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    # Size cap before yaml.safe_load — even safe_load can spend significant
    # CPU on a multi-MB document, and there's no legitimate scarguard.yml
    # remotely close to this size.
    raw_bytes = len(raw_yaml.encode("utf-8"))
    if raw_bytes > _MAX_RAW_YAML_BYTES:
        log.warning(
            "Rejecting raw-YAML save of %d bytes (cap %d)",
            raw_bytes, _MAX_RAW_YAML_BYTES,
        )
        return HTMLResponse(
            f"<h1>413 — Payload too large</h1>"
            f"<p>Config exceeds {_MAX_RAW_YAML_BYTES // 1000} KB cap "
            f"(received {raw_bytes // 1000} KB). "
            f"Keep your scarguard.yml lean.</p>",
            status_code=413,
        )
    error = None
    saved = False
    warnings: list[str] = []
    raw_cfg: dict = {}
    # Reject saves that still contain redacted placeholders — the user
    # must reveal secrets before editing raw YAML, otherwise they would
    # overwrite real credentials with "***REDACTED***".
    if REDACTED_PLACEHOLDER in raw_yaml:
        error = (
            "Cannot save: the YAML contains redacted placeholders "
            "(***REDACTED***). Click 'Reveal secrets' first, then edit and save."
        )
        raw_cfg = yaml.safe_load(raw_yaml) or {}
        if not isinstance(raw_cfg, dict):
            raw_cfg = {}
    else:
        try:
            raw_cfg = yaml.safe_load(raw_yaml)
            if not isinstance(raw_cfg, dict):
                raise ValueError("Config must be a YAML mapping")
            config_store.save(raw_cfg)
            saved = True
            raw_yaml = yaml.dump(raw_cfg, default_flow_style=False, sort_keys=False)
            warnings = _find_orphan_references(raw_cfg)
            audit.record_request(
                request,
                action="config.save",
                resource="scarguard.yml",
                details={"form": "raw_yaml", "orphan_warnings": len(warnings)},
            )
        except Exception:
            # Same scrubbing pattern as save_structured_config — don't surface raw
            # exception text in the rendered template. Admin-only endpoint, but
            # CodeQL flags the sink and the operator gets a request ID to grep.
            req_id = uuid.uuid4().hex[:8]
            log.exception("raw YAML config save failed [%s]", req_id)
            error = f"Unable to save config (request_id={req_id})"

    cfg = _parse_cfg(raw_cfg)

    # Redact secrets in the template response — same as config_page().
    _redact_parsed_cfg(cfg)
    raw_cfg_for_render = redact_config(raw_cfg)
    raw_yaml = yaml.dump(raw_cfg_for_render, default_flow_style=False, sort_keys=False)

    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "raw_yaml": raw_yaml,
            "saved": saved,
            "error": error,
            "warnings": warnings,
            "cfg": cfg,
            "cameras_json": _cameras_json(cfg.cameras),
            "channels_json": _channels_json(raw_cfg_for_render),
            "groups_json": _groups_json(raw_cfg_for_render),
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
) -> Response:
    """Upload or paste TLS certificate and key files.  Admin only.

    Accepts either file uploads (cert_file, key_file) or pasted PEM text
    (cert_pem, key_pem).  Files are written to /config/certs/ which is
    mounted from the scarguard-config volume.
    """
    gate = require_admin(request, is_api=True)
    if not isinstance(gate, dict):
        return gate
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

    # Validate everything before writing anything to avoid partial-write
    # on a half-bad upload.
    errors: list[str] = []
    cert_ok = False
    key_ok = False

    if cert_data:
        if len(cert_data) > _MAX_CERT_SIZE:
            errors.append("Certificate file too large (max 64 KB)")
        elif b"-----BEGIN" not in cert_data:
            errors.append("Certificate does not appear to be PEM-encoded")
        else:
            cert_ok = True

    if key_data:
        if len(key_data) > _MAX_CERT_SIZE:
            errors.append("Key file too large (max 64 KB)")
        elif b"-----BEGIN" not in key_data:
            errors.append("Key does not appear to be PEM-encoded")
        else:
            key_ok = True

    if errors:
        return JSONResponse({"ok": False, "error": "; ".join(errors)}, status_code=400)

    # All validation passed — write files. Filenames are hardcoded literals
    # so no user-controlled value can reach the destination path. (Earlier
    # iterations routed the names through a (name, data) tuple list, which
    # CodeQL's taint tracker over-approximated as path-injection because it
    # could not prove the first tuple element was always literal. Inlining
    # the writes makes the literal nature obvious to both humans and the
    # analyzer — issue #95.)
    written: list[str] = []
    if cert_ok and cert_data is not None:
        (_CERTS_DIR / "cert.pem").write_bytes(cert_data)
        written.append("cert.pem")
    if key_ok and key_data is not None:
        key_path = _CERTS_DIR / "key.pem"
        key_path.write_bytes(key_data)
        key_path.chmod(0o600)
        written.append("key.pem")

    log.info("TLS cert files uploaded: %s", ", ".join(written))
    return JSONResponse({"ok": True, "written": written})


# ── Test notification ─────────────────────────────────────────────────────────

_SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", "/data/snapshots"))
_TEST_IMAGE = Path(__file__).resolve().parent.parent / "static" / "test-fish.png"
_DETECTIONS_CHANNEL = "scarguard:detections"


@router.post("/test-notification", response_class=JSONResponse)
async def send_test_notification(request: Request) -> Response:
    """Send a test notification to a named channel via the notifier.

    Admin only — viewers are blocked because this fires real outbound
    traffic (Discord webhook, email SMTP, etc.).
    """
    gate = require_admin(request, is_api=True)
    if not isinstance(gate, dict):
        return gate
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
