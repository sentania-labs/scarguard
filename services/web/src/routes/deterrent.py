"""Deterrent admin page — Tuya credentials, device management, test-fire, status."""

from __future__ import annotations

import json
import logging
import math
import os
import time as _time
import uuid
from pathlib import Path
from typing import Any

import actuation_db
import config_store
import redis.asyncio as aioredis
from config_redact import REDACTED_PLACEHOLDER
from deterrent_safety import (
    DEFAULT_TEST_FIRE_SEC,
    MAX_TEST_FIRE_SEC,
    MIN_ACTUATION_SEC,
)
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from rate_limit_dep import rate_limit
from route_auth import has_admin_access, require_admin, require_viewer
from sse_limiter import SSETooManyStreams, sse_connection
from starlette.responses import Response
from template_json import safe_json_dumps

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

    groups: list[dict[str, Any]] = act.get("groups", [])
    if not isinstance(groups, list):
        groups = []

    # Which cameras reference each group (for "used by" UI chips)?
    group_usage: dict[str, list[str]] = {}
    for cam in cfg.get("cameras", []):
        if not isinstance(cam, dict):
            continue
        cam_name = cam.get("name", "")
        for rule in cam.get("deterrent_rules", []):
            if not isinstance(rule, dict):
                continue
            for g_name in rule.get("groups", []):
                group_usage.setdefault(g_name, []).append(cam_name)

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
            "devices_json": safe_json_dumps(devices),
            "groups": groups,
            "groups_json": safe_json_dumps(groups),
            "group_usage_json": safe_json_dumps(group_usage),
            "defaults": defaults,
            "defaults_json": safe_json_dumps(defaults),
            "battery_monitor": battery_monitor,
            "battery_monitor_json": safe_json_dumps(battery_monitor),
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

    # Compute the authoritative set of currently-registered device names.
    # Used to filter out orphaned references in group.devices so a deleted
    # device can't leave a group silently broken (deterrent worker would
    # fail to resolve the group and skip firing).  Pulls from the just-
    # persisted devices list if this request included one, otherwise from
    # whatever's already on disk.
    registry_devices = existing_act.get("devices", [])
    if not isinstance(registry_devices, list):
        registry_devices = []
    registered_names: set[str] = {
        d["name"] for d in registry_devices
        if isinstance(d, dict) and isinstance(d.get("name"), str)
    }

    # Update groups — list of {name, devices[], cooldown_seconds,
    # optional *_range overrides}.  Validates and coerces types; unknown
    # extra fields are dropped; device names unknown to the registry are
    # filtered out (orphan rejection) with a warning log.
    groups_input = body.get("groups")
    if isinstance(groups_input, list):
        clean_groups: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for g in groups_input:
            if not isinstance(g, dict):
                continue
            name = str(g.get("name", "")).strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            devices_list = g.get("devices", [])
            if not isinstance(devices_list, list):
                devices_list = []
            requested_names = [str(d) for d in devices_list if d]
            known_devices = [n for n in requested_names if n in registered_names]
            orphaned = [n for n in requested_names if n not in registered_names]
            if orphaned:
                log.warning(
                    "Group %r references unknown device name(s) %s — dropping",
                    name, orphaned,
                )
            entry: dict[str, Any] = {
                "name": name,
                "devices": known_devices,
                "cooldown_seconds": int(g.get("cooldown_seconds", 60)),
            }
            for opt_key in (
                "device_count_range",
                "spray_duration_range",
                "inter_device_delay_range",
                "pre_delay_range",
            ):
                if opt_key in g and g[opt_key]:
                    entry[opt_key] = g[opt_key]
            clean_groups.append(entry)
        existing_act["groups"] = clean_groups

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
FORCE_OFF_CHANNEL = "scarguard:deterrent:force-off"
FORCE_OFF_RESULT_PREFIX = "scarguard:deterrent:force-off:result:"


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


@router.post(
    "/test-fire", response_class=JSONResponse,
    dependencies=[Depends(rate_limit("test-fire", capacity=10, window_seconds=60))],
)
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
    if not device_id:
        return JSONResponse({"ok": False, "error": "device_id is required"}, status_code=400)

    raw_duration = body.get("duration_sec", DEFAULT_TEST_FIRE_SEC)
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "duration_sec must be a number"},
            status_code=400,
        )
    if math.isnan(duration) or math.isinf(duration):
        return JSONResponse(
            {"ok": False, "error": "duration_sec must be finite"},
            status_code=400,
        )
    if duration < MIN_ACTUATION_SEC or duration > MAX_TEST_FIRE_SEC:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"duration_sec must be between {MIN_ACTUATION_SEC} and "
                    f"{MAX_TEST_FIRE_SEC} seconds"
                ),
            },
            status_code=400,
        )

    result = await _redis_request(
        TEST_FIRE_CHANNEL, TEST_FIRE_RESULT_PREFIX,
        {"device_id": device_id, "duration_sec": duration},
    )
    status_code = 200 if result.get("ok") else 502
    return JSONResponse(result, status_code=status_code)


@router.post(
    "/force-off", response_class=JSONResponse,
    dependencies=[Depends(rate_limit("force-off", capacity=5, window_seconds=60))],
)
async def force_off(request: Request) -> Response:
    """Emergency OFF — force every configured device OFF regardless of state.

    Admin only. No duration, no retry logic — the deterrent service sends
    OFF to each device and returns a per-device success map. Use this when
    a sprinkler is stuck on or you suspect the actuation state has drifted.
    """
    gate = require_admin(request, is_api=True)
    if not isinstance(gate, dict):
        return gate

    result = await _redis_request(
        FORCE_OFF_CHANNEL, FORCE_OFF_RESULT_PREFIX, {},
        timeout_sec=30.0,
    )
    status_code = 200 if result.get("ok") else 502
    return JSONResponse(result, status_code=status_code)


@router.get("/latency-summary", response_class=JSONResponse)
async def latency_summary(request: Request) -> Response:
    """Return p50/p95 latency percentiles over the last N actuations.

    Viewer-accessible — diagnostic data, no secrets.
    """
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate
    last_n = int(request.query_params.get("last_n", 100))
    last_n = max(1, min(last_n, 1000))
    return JSONResponse(actuation_db.get_latency_summary(last_n))


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


# ── Deterrent stuck-device SSE stream ───────────────────────────────────────
# Separate router so the endpoint lives at /deterrent-stuck/stream (global,
# not admin-prefixed — the banner shows on every page via base.html).

STUCK_CHANNEL = "scarguard:deterrent:stuck"

stuck_router = APIRouter(prefix="/deterrent-stuck")


@stuck_router.get("/stream")
async def stuck_stream(request: Request) -> StreamingResponse:
    """SSE stream — pushes stuck-device events for the global sticky banner."""
    cfg = config_store.load_cached()
    redis_cfg = cfg.get("redis", {})
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))

    user = getattr(request.state, "user", None)
    user_id = user["user_id"] if user else "anon"

    async def generator():
        client = aioredis.Redis(
            host=host, port=port,
            password=os.environ.get("REDIS_PASSWORD", "") or None,
            decode_responses=True,
        )
        try:
            async with sse_connection(client, user_id):
                pubsub = client.pubsub()
                await pubsub.subscribe(STUCK_CHANNEL)
                yield ": connected\n\n"
                try:
                    while not await request.is_disconnected():
                        message = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=15.0,
                        )
                        if message is None:
                            yield ": keepalive\n\n"
                            continue
                        if message["type"] != "message":
                            continue
                        try:
                            payload = json.loads(message["data"])
                        except json.JSONDecodeError:
                            continue
                        yield f"event: stuck\ndata: {json.dumps(payload)}\n\n"
                finally:
                    await pubsub.unsubscribe(STUCK_CHANNEL)
        except SSETooManyStreams:
            yield "event: error\ndata: Too many active streams\n\n"
        finally:
            await client.aclose()

    return StreamingResponse(generator(), media_type="text/event-stream")
