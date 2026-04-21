import logging
import os
from datetime import date as dt_date
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import actuation_db
import config_store
import db
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from route_auth import current_role, has_admin_access

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_REARM_KEY = "scarguard:rearm_at"


def _is_admin(request: Request) -> bool:
    """Legacy shim. Prefer route_auth.has_admin_access() in new code."""
    return has_admin_access(request)


def _redis_client(cfg: dict) -> Any:
    try:
        import redis.asyncio as aioredis

        rc = cfg.get("redis", {})
        pw = os.environ.get("REDIS_PASSWORD", "") or None
        return aioredis.Redis(
            host=rc.get("host", "redis"),
            port=int(rc.get("port", 6379)),
            password=pw,
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
        await r.close()


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
        await r.close()


async def _set_rearm_at(cfg: dict, ts: str) -> None:
    r = _redis_client(cfg)
    if r is None:
        return
    try:
        await r.set(_REARM_KEY, ts)
    except Exception:
        log.warning("Failed to set rearm_at in Redis")
    finally:
        await r.close()


async def _clear_rearm_at(cfg: dict) -> None:
    r = _redis_client(cfg)
    if r is None:
        return
    try:
        await r.delete(_REARM_KEY)
    except Exception:
        log.warning("Failed to clear rearm_at in Redis")
    finally:
        await r.close()


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

    # Enabled notification channels for "send snapshot" feature
    notif_channels: list[str] = []
    raw_channels = cfg.get("notifications", {}).get("channels", [])
    if isinstance(raw_channels, list):
        for ch in raw_channels:
            if isinstance(ch, dict) and ch.get("enabled", True) and ch.get("name"):
                notif_channels.append(ch["name"])

    role = current_role(request)
    deterrent_ctx = _deterrent_context(cfg, can_toggle=role in ("user", "admin"))

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "armed": cfg.get("system", {}).get("armed", True),
            "rearm_at": rearm_at,
            "is_admin": _is_admin(request),
            "user_role": role,
            "cameras": cameras,
            "camera_health": camera_health,
            "total_events": total,
            "latest": latest_dict,
            "model_path": cfg.get("detection", {}).get("model_path", "—"),
            "schedule": _get_schedule_info(cfg),
            "training_nudge": training_nudge,
            "notif_channels": notif_channels,
            "deterrent": deterrent_ctx,
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
async def arm(request: Request) -> Response:
    # Arming is a write action — viewers and unauth'd users are rejected.
    # Regular users (role=user) historically could hit this route; preserve
    # that so arm/disarm is symmetric for them.
    role = current_role(request)
    if role not in ("user", "admin"):
        return await _arm_badge(
            request,
            armed=config_store.load().get("system", {}).get("armed", True),
        )
    cfg = config_store.load()
    config_store.set_armed(True)
    await _clear_rearm_at(cfg)
    return await _arm_badge(request, armed=True)


@router.post("/disarm", response_class=HTMLResponse)
async def disarm(request: Request) -> Response:
    # Viewers are read-only — refuse the disarm action outright and render
    # the current arm badge unchanged.  Regular users can still disarm with
    # the auto-rearm behaviour.
    role = current_role(request)
    if role not in ("user", "admin"):
        return await _arm_badge(
            request,
            armed=config_store.load().get("system", {}).get("armed", True),
        )
    cfg = config_store.load()
    config_store.set_armed(False)
    rearm_at: str | None = None
    if role == "admin":
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


def _humanize_since(iso_ts: str, tz_name: str) -> str:
    """Return a terse "Xm ago" / "Xh ago" string for *iso_ts*; falls back to local time."""
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return iso_ts
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return _to_local(iso_ts, tz_name)


def _deterrent_context(cfg: dict, *, can_toggle: bool) -> dict[str, Any]:
    """Build the template context for the dashboard deterrent widget.

    Cooldown remaining is the MAX of the global cooldown
    (``deterrent.defaults.cooldown_seconds``) and the per-group cooldown
    of the group that fired last.  Both layers gate future actuations of
    that same group, so showing the effective block is the accurate
    signal — reporting only the global would say "Ready" while a
    longer-cooldown group is still blocked.

    Derived from the persisted wall-clock timestamp in actuation_db, not
    the deterrent service's in-memory monotonic clock.  The sub-second
    drift between fire and DB write is immaterial for a status readout.
    Note: the UI reflects file-state immediately on toggle; the
    deterrent service picks up the change when its config watcher fires
    (typically <1 s).
    """
    det = cfg.get("deterrent", {}) if isinstance(cfg.get("deterrent"), dict) else {}
    enabled = bool(det.get("enabled", False))
    # Mirror the deterrent service's Pydantic defaults (60s) — see
    # services/deterrent/src/actuation_models.py ActuationDefaults /
    # GroupConfig.  A missing value in YAML doesn't mean "no cooldown";
    # the worker applies 60s, so the widget must match or it lies.
    global_cd = int(det.get("defaults", {}).get("cooldown_seconds", 60) or 60)
    tz_name = cfg.get("system", {}).get("timezone") or "UTC"

    group_cd_by_name: dict[str, int] = {}
    for g in det.get("groups", []) or []:
        if isinstance(g, dict) and g.get("name"):
            group_cd_by_name[str(g["name"])] = int(g.get("cooldown_seconds", 60) or 60)

    latest = actuation_db.get_latest_event()
    last_fire: dict[str, Any] | None = None
    cooldown_remaining = 0
    if latest and latest.get("timestamp"):
        iso_ts = str(latest["timestamp"])
        group_name = latest.get("group_name") or ""
        last_fire = {
            "iso": iso_ts,
            "relative": _humanize_since(iso_ts, tz_name),
            "trigger_class": latest.get("trigger_class", "?"),
            "trigger_camera": latest.get("trigger_camera", "?"),
            "trigger_confidence": float(latest.get("trigger_confidence") or 0.0),
            "group_name": group_name,
        }
        effective_cd = max(global_cd, group_cd_by_name.get(group_name, 0))
        if effective_cd > 0:
            try:
                dt = datetime.fromisoformat(iso_ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - dt).total_seconds()
                cooldown_remaining = max(0, int(effective_cd - elapsed))
            except (ValueError, TypeError):
                cooldown_remaining = 0

    return {
        "enabled": enabled,
        "can_toggle": can_toggle,
        "last_fire": last_fire,
        "cooldown_remaining": cooldown_remaining,
    }


async def _deterrent_partial(request: Request) -> HTMLResponse:
    """Render the deterrent status partial for the current user."""
    cfg = config_store.load()
    role = current_role(request)
    can_toggle = role in ("user", "admin")
    ctx = _deterrent_context(cfg, can_toggle=can_toggle)
    return templates.TemplateResponse(
        request, "partials/deterrent_status.html", {"deterrent": ctx},
    )


@router.get("/deterrent-status", response_class=HTMLResponse)
async def deterrent_status(request: Request) -> Response:
    """HTMX poll endpoint — returns the deterrent widget fragment."""
    return await _deterrent_partial(request)


@router.post("/deterrent-enable", response_class=HTMLResponse)
async def deterrent_enable(request: Request) -> Response:
    """Flip deterrent.enabled=true.  Viewers are rejected; widget re-renders unchanged."""
    role = current_role(request)
    if role not in ("user", "admin"):
        return await _deterrent_partial(request)
    config_store.set_deterrent_enabled(True)
    log.info("Deterrent enabled via dashboard (role=%s)", role)
    return await _deterrent_partial(request)


@router.post("/deterrent-disable", response_class=HTMLResponse)
async def deterrent_disable(request: Request) -> Response:
    """Flip deterrent.enabled=false.  Viewers are rejected; widget re-renders unchanged."""
    role = current_role(request)
    if role not in ("user", "admin"):
        return await _deterrent_partial(request)
    config_store.set_deterrent_enabled(False)
    log.info("Deterrent disabled via dashboard (role=%s)", role)
    return await _deterrent_partial(request)
