"""Deterrent admin page — Tuya credentials, device management, test-fire, status."""

from __future__ import annotations

import json
import logging
import os
import time as _time
import uuid
from pathlib import Path
from typing import Any

import config_store
import redis.asyncio as aioredis
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

    defaults: dict[str, Any] = act.get("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}
    battery_monitor: dict[str, Any] = act.get("battery_monitor", {})
    if not isinstance(battery_monitor, dict):
        battery_monitor = {}

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
            "defaults": defaults,
            "defaults_json": json.dumps(defaults),
            "battery_monitor": battery_monitor,
            "battery_monitor_json": json.dumps(battery_monitor),
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

    # Update tuya credentials — only persist if both required fields are
    # present to avoid writing a partial block that breaks service startup.
    # Redacted placeholder values are ignored (viewer submitted the form).
    tuya_input = body.get("tuya", {})
    if isinstance(tuya_input, dict):
        existing_tuya = existing_act.get("tuya", {})
        if not isinstance(existing_tuya, dict):
            existing_tuya = {}
        api_key = tuya_input.get("api_key", "")
        api_secret = tuya_input.get("api_secret", "")
        api_region = tuya_input.get("api_region", "us")

        # Resolve effective values: use input unless it's redacted/empty,
        # in which case keep the existing value.
        eff_key = api_key if (api_key and api_key != REDACTED_PLACEHOLDER) else existing_tuya.get("api_key", "")
        eff_secret = api_secret if (api_secret and api_secret != REDACTED_PLACEHOLDER) else existing_tuya.get("api_secret", "")
        eff_region = api_region if api_region else existing_tuya.get("api_region", "us")

        if eff_key and eff_secret:
            existing_tuya["api_key"] = eff_key
            existing_tuya["api_secret"] = eff_secret
            existing_tuya["api_region"] = eff_region
            existing_act["tuya"] = existing_tuya
        elif not eff_key and not eff_secret:
            # Both cleared — remove tuya block entirely
            existing_act.pop("tuya", None)
        # else: one field set, one empty — keep existing tuya unchanged

    # Update devices list — preserve dp_code overrides not exposed in UI
    devices_input = body.get("devices")
    if isinstance(devices_input, list):
        # Build lookup of existing dp_code by device_id
        existing_devices = existing_act.get("devices", [])
        dp_code_lookup: dict[str, str] = {}
        if isinstance(existing_devices, list):
            for ed in existing_devices:
                if isinstance(ed, dict) and ed.get("dp_code") and ed.get("device_id"):
                    dp_code_lookup[ed["device_id"]] = ed["dp_code"]

        clean_devices = []
        for d in devices_input:
            if not isinstance(d, dict):
                continue
            name = str(d.get("name", "")).strip()
            device_id = str(d.get("device_id", "")).strip()
            if not name or not device_id:
                continue
            dev_entry: dict[str, Any] = {
                "name": name,
                "device_id": device_id,
                "type": d.get("type", "sprinkler"),
                "enabled": bool(d.get("enabled", True)),
            }
            # Carry forward dp_code from existing config
            if device_id in dp_code_lookup:
                dev_entry["dp_code"] = dp_code_lookup[device_id]
            clean_devices.append(dev_entry)
        existing_act["devices"] = clean_devices

    # Update defaults
    defaults_input = body.get("defaults")
    if isinstance(defaults_input, dict):
        existing_defaults = existing_act.get("defaults", {})
        if not isinstance(existing_defaults, dict):
            existing_defaults = {}
        # Merge individual fields so partial updates work
        for key in ("cooldown_seconds", "device_count_range", "spray_duration_range",
                     "inter_device_delay_range", "pre_delay_range"):
            if key in defaults_input:
                existing_defaults[key] = defaults_input[key]
        existing_act["defaults"] = existing_defaults

    # Update battery monitor
    battery_input = body.get("battery_monitor")
    if isinstance(battery_input, dict):
        existing_batt = existing_act.get("battery_monitor", {})
        if not isinstance(existing_batt, dict):
            existing_batt = {}
        for key in ("enabled", "check_interval_hours", "alert_threshold_percent"):
            if key in battery_input:
                existing_batt[key] = battery_input[key]
        existing_act["battery_monitor"] = existing_batt

    existing["deterrent"] = existing_act
    config_store.save(existing)

    log.info("Deterrent config saved — %d device(s)", len(existing_act.get("devices", [])))
    return JSONResponse({"ok": True})


# ── Redis request/response helpers ────────────────────────────────────────────

TEST_FIRE_CHANNEL = "scarguard:deterrent:test-fire"
TEST_FIRE_RESULT_PREFIX = "scarguard:deterrent:test-fire:result:"
STATUS_REQUEST_CHANNEL = "scarguard:deterrent:status-request"
STATUS_RESULT_PREFIX = "scarguard:deterrent:status:result:"


def _redis_params() -> dict[str, Any]:
    cfg = config_store.load_cached()
    redis_cfg = cfg.get("redis", {})
    return {
        "host": redis_cfg.get("host", "redis"),
        "port": int(redis_cfg.get("port", 6379)),
        "password": os.environ.get("REDIS_PASSWORD", "") or None,
        "decode_responses": True,
    }


async def _redis_request(
    request_channel: str,
    result_prefix: str,
    payload: dict[str, Any],
    timeout_sec: float = 15.0,
) -> dict[str, Any]:
    """Publish a request and wait for the response (Redis request/response)."""
    request_id = uuid.uuid4().hex
    payload["request_id"] = request_id
    result_channel = f"{result_prefix}{request_id}"

    params = _redis_params()
    client = aioredis.Redis(**params)
    try:
        pubsub = client.pubsub()
        await pubsub.subscribe(result_channel)
        await client.publish(request_channel, json.dumps(payload))

        deadline = _time.monotonic() + timeout_sec
        while _time.monotonic() < deadline:
            remaining = max(0.5, deadline - _time.monotonic())
            msg = await pubsub.get_message(timeout=min(remaining, 2.0))
            if msg and msg["type"] == "message":
                try:
                    return json.loads(msg["data"])
                except (json.JSONDecodeError, TypeError):
                    continue

        return {"ok": False, "error": "Request timed out — deterrent service may not be running"}
    finally:
        await pubsub.unsubscribe(result_channel)
        await client.close()


@router.post("/test-fire", response_class=JSONResponse)
async def test_fire(request: Request) -> Response:
    """Fire a single device for testing — admin only."""
    gate = require_admin(request, is_api=True)
    if not isinstance(gate, dict):
        return gate

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    device_id = body.get("device_id", "")
    try:
        duration = float(body.get("duration_sec", 3.0))
    except (TypeError, ValueError):
        duration = 3.0
    if not device_id:
        return JSONResponse({"ok": False, "error": "device_id is required"}, status_code=400)

    result = await _redis_request(
        TEST_FIRE_CHANNEL, TEST_FIRE_RESULT_PREFIX,
        {"device_id": device_id, "duration_sec": duration},
    )
    status_code = 200 if result.get("ok") else 502
    return JSONResponse(result, status_code=status_code)


@router.get("/device-status", response_class=JSONResponse)
async def device_status(request: Request) -> Response:
    """Query live device status from the deterrent service."""
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate

    result = await _redis_request(
        STATUS_REQUEST_CHANNEL, STATUS_RESULT_PREFIX, {},
    )
    status_code = 200 if result.get("ok") else 502
    return JSONResponse(result, status_code=status_code)
