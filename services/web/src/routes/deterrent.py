"""Deterrent admin page — Tuya credentials and device management."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import config_store
from config_redact import REDACTED_PLACEHOLDER
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from route_auth import has_admin_access, require_admin, require_viewer
from starlette.responses import Response

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/deterrent")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
async def deterrent_page(request: Request) -> Response:
    """Deterrent config page — Tuya credentials and device list."""
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate

    cfg = config_store.load()
    act: dict[str, Any] = cfg.get("deterrent", {})
    if not isinstance(act, dict):
        act = {}

    read_only = not has_admin_access(request)
    tuya: dict[str, Any] = act.get("tuya", {})
    if not isinstance(tuya, dict):
        tuya = {}
    devices: list[dict[str, Any]] = act.get("devices", [])
    if not isinstance(devices, list):
        devices = []

    # Redact for viewers
    if read_only:
        if tuya.get("api_key"):
            tuya["api_key"] = REDACTED_PLACEHOLDER
        if tuya.get("api_secret"):
            tuya["api_secret"] = REDACTED_PLACEHOLDER

    return templates.TemplateResponse(
        request,
        "deterrent.html",
        {
            "read_only": read_only,
            "enabled": act.get("enabled", False),
            "tuya": tuya,
            "devices": devices,
            "devices_json": json.dumps(devices),
        },
    )


@router.post("", response_class=JSONResponse)
async def save_deterrent(request: Request) -> Response:
    """Save deterrent config — admin only."""
    gate = require_admin(request, is_api=True)
    if not isinstance(gate, dict):
        return gate

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    existing = config_store.load()
    existing_act: dict[str, Any] = existing.get("deterrent", {})
    if not isinstance(existing_act, dict):
        existing_act = {}

    # Update tuya credentials (skip if redacted)
    tuya_input = body.get("tuya", {})
    if isinstance(tuya_input, dict):
        existing_tuya = existing_act.get("tuya", {})
        if not isinstance(existing_tuya, dict):
            existing_tuya = {}
        for key in ("api_key", "api_secret", "api_region"):
            val = tuya_input.get(key, "")
            if val and val != REDACTED_PLACEHOLDER:
                existing_tuya[key] = val
            elif key == "api_region" and val:
                existing_tuya[key] = val
        existing_act["tuya"] = existing_tuya

    # Update devices list
    devices_input = body.get("devices")
    if isinstance(devices_input, list):
        clean_devices = []
        for d in devices_input:
            if not isinstance(d, dict):
                continue
            name = str(d.get("name", "")).strip()
            device_id = str(d.get("device_id", "")).strip()
            if not name or not device_id:
                continue
            clean_devices.append({
                "name": name,
                "device_id": device_id,
                "type": d.get("type", "sprinkler"),
                "enabled": bool(d.get("enabled", True)),
            })
        existing_act["devices"] = clean_devices

    existing["deterrent"] = existing_act
    config_store.save(existing)

    log.info("Deterrent config saved — %d device(s)", len(existing_act.get("devices", [])))
    return JSONResponse({"ok": True})
