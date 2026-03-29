import logging
from datetime import date as dt_date
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config_store
import db
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_REARM_KEY = "scarguard:rearm_at"


def _is_admin(request: Request) -> bool:
    user = getattr(request.state, "user", None)
    return bool(user and user.get("is_admin"))


def _redis_client(cfg: dict) -> Any:
    try:
        import redis.asyncio as aioredis

        rc = cfg.get("redis", {})
        return aioredis.Redis(
            host=rc.get("host", "redis"),
            port=int(rc.get("port", 6379)),
            decode_responses=True,
        )
    except Exception:
        log.warning("Failed to create Redis client")
        return None


async def _get_rearm_at(cfg: dict) -> str | None:
    r = _redis_client(cfg)
    if r is None:
        return None
    try:
        val: str | None = await r.get(_REARM_KEY)
        return val
    except Exception:
        log.warning("Failed to read rearm_at from Redis")
        return None
    finally:
        await r.aclose()


async def _get_camera_health(cfg: dict) -> dict:
    """Read camera health status from the stats Redis key."""
    r = _redis_client(cfg)
    if r is None:
        return {}
    try:
        import json

        data = await r.get("scarguard:stats")
        if data:
            stats = json.loads(data)
            return stats.get("camera_health", {})
        return {}
    except Exception:
        log.warning("Failed to read camera health from Redis")
        return {}
    finally:
        await r.aclose()


async def _set_rearm_at(cfg: dict, ts: str) -> None:
    r = _redis_client(cfg)
    if r is None:
        return
    try:
        await r.set(_REARM_KEY, ts)
    except Exception:
        log.warning("Failed to set rearm_at in Redis")
    finally:
        await r.aclose()


async def _clear_rearm_at(cfg: dict) -> None:
    r = _redis_client(cfg)
    if r is None:
        return
    try:
        await r.delete(_REARM_KEY)
    except Exception:
        log.warning("Failed to clear rearm_at in Redis")
    finally:
        await r.aclose()


def _parse_time(s: str) -> dt_time | None:
    try:
        parts = s.strip().split(":")
        return dt_time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError, AttributeError):
        return None


def _localtime_to_utc(d: dt_date, t: dt_time, tz_name: str) -> datetime:
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        tz = ZoneInfo("UTC")
    return datetime.combine(d, t, tzinfo=tz).astimezone(timezone.utc)


def _get_schedule_info(cfg: dict) -> dict:
    """Compute schedule status for display on the dashboard."""
    sys_cfg = cfg.get("system", {})
    sched_cfg = sys_cfg.get("schedule") or {}
    tz_name = sys_cfg.get("timezone") or "UTC"

    arm_str = sched_cfg.get("arm_time") or ""
    disarm_str = sched_cfg.get("disarm_time") or ""
    use_solar = bool(sched_cfg.get("use_solar", False))
    lat = sched_cfg.get("latitude")
    lon = sched_cfg.get("longitude")

    cfg_enabled = bool(sched_cfg.get("enabled", False))
    enabled = cfg_enabled and bool(
        (arm_str and disarm_str) or (use_solar and lat is not None and lon is not None)
    )
    if not enabled:
        return {"enabled": False}

    arm_t = _parse_time(arm_str) if arm_str else None
    disarm_t = _parse_time(disarm_str) if disarm_str else None

    now_utc = datetime.now(timezone.utc)

    def _solar(d: dt_date, kind: str) -> datetime | None:
        try:
            from astral import LocationInfo
            from astral.sun import sun as astral_sun
            loc = LocationInfo(latitude=float(lat), longitude=float(lon))  # type: ignore[arg-type]
            s = astral_sun(loc.observer, date=d, tzinfo=timezone.utc)
            result: datetime = s[kind]
            return result
        except Exception as exc:
            log.warning("Failed to compute solar %s for %s: %s", kind, d, exc)
            return None

    def get_arm(d: dt_date) -> datetime | None:
        if use_solar:
            return _solar(d, "sunrise")
        if arm_t:
            return _localtime_to_utc(d, arm_t, tz_name)
        return None

    def get_disarm(d: dt_date) -> datetime | None:
        if use_solar:
            return _solar(d, "sunset")
        if disarm_t:
            return _localtime_to_utc(d, disarm_t, tz_name)
        return None

    # Find next transition within 3 days
    next_t: tuple[datetime, bool] | None = None
    for days_ahead in range(3):
        d = now_utc.date() + timedelta(days=days_ahead)
        candidates = []
        for target, getter in [(True, get_arm), (False, get_disarm)]:
            t = getter(d)
            if t is not None and t > now_utc:
                candidates.append((t, target))
        if candidates:
            candidates.sort(key=lambda x: x[0])
            next_t = candidates[0]
            break

    if next_t:
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, KeyError):
            tz = ZoneInfo("UTC")
        next_time_local = next_t[0].astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
        next_action = "Arm" if next_t[1] else "Disarm"
    else:
        next_time_local = "—"
        next_action = "—"

    return {
        "enabled": True,
        "use_solar": use_solar,
        "arm_time": arm_str,
        "disarm_time": disarm_str,
        "next_time": next_time_local,
        "next_action": next_action,
    }


def _to_local(iso_str: str, tz_name: str) -> str:
    """Convert a UTC ISO 8601 string to a formatted local-time string."""
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError, TypeError):
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
    tz_name = cfg.get("system", {}).get("timezone") or "UTC"
    latest_dict = None
    if latest:
        latest_dict = dict(latest)
        latest_dict["display_timestamp"] = _to_local(
            latest_dict.get("timestamp", ""), tz_name
        )
    rearm_at = await _get_rearm_at(cfg)
    camera_health = await _get_camera_health(cfg)

    # Training data nudge
    training_nudge = None
    nudge_threshold = cfg.get("system", {}).get("training_nudge_threshold", 100)
    last_export = db.get_app_state("last_export_date")
    labeled = db.count_labeled_since(last_export)
    if labeled["total"] >= nudge_threshold:
        training_nudge = {
            "total": labeled["total"],
            "by_class": labeled["by_class"],
            "last_export": last_export,
        }

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "armed": cfg.get("system", {}).get("armed", True),
            "rearm_at": rearm_at,
            "is_admin": _is_admin(request),
            "cameras": cameras,
            "camera_health": camera_health,
            "total_events": total,
            "latest": latest_dict,
            "model_path": cfg.get("detection", {}).get("model_path", "—"),
            "schedule": _get_schedule_info(cfg),
            "training_nudge": training_nudge,
        },
    )


@router.get("/arm-status", response_class=HTMLResponse)
async def arm_status(request: Request):
    """Return the arm badge fragment; used by HTMX polling."""
    cfg = config_store.load()
    armed = cfg.get("system", {}).get("armed", True)
    rearm_at = await _get_rearm_at(cfg)
    return await _arm_badge(request, armed=armed, rearm_at=rearm_at)


@router.post("/arm", response_class=HTMLResponse)
async def arm(request: Request):
    cfg = config_store.load()
    config_store.set_armed(True)
    await _clear_rearm_at(cfg)
    return await _arm_badge(request, armed=True)


@router.post("/disarm", response_class=HTMLResponse)
async def disarm(request: Request):
    cfg = config_store.load()
    config_store.set_armed(False)
    rearm_at: str | None = None
    if _is_admin(request):
        await _clear_rearm_at(cfg)
    else:
        rearm_minutes = (
            cfg.get("system", {}).get("auth", {}).get("nonadmin_rearm_minutes", 30)
        )
        if isinstance(rearm_minutes, int) and rearm_minutes > 0:
            rearm_time = datetime.now(timezone.utc) + timedelta(minutes=rearm_minutes)
            rearm_at = rearm_time.isoformat()
            await _set_rearm_at(cfg, rearm_at)
    return await _arm_badge(request, armed=False, rearm_at=rearm_at)


@router.post("/cancel-rearm", response_class=HTMLResponse)
async def cancel_rearm(request: Request):
    """Admin-only: cancel a pending non-admin auto-rearm."""
    cfg = config_store.load()
    armed = cfg.get("system", {}).get("armed", True)
    if not _is_admin(request):
        return await _arm_badge(request, armed=armed)
    await _clear_rearm_at(cfg)
    return await _arm_badge(request, armed=armed)


async def _arm_badge(
    request: Request,
    *,
    armed: bool,
    rearm_at: str | None = None,
    is_admin: bool | None = None,
) -> HTMLResponse:
    """Return just the status badge fragment for HTMX swap."""
    if is_admin is None:
        is_admin = _is_admin(request)
    return templates.TemplateResponse(
        request,
        "partials/arm_badge.html",
        {"armed": armed, "rearm_at": rearm_at, "is_admin": is_admin},
    )
