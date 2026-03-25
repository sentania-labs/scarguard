import logging
from datetime import date as dt_date
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config_store
import db
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


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
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "armed": cfg.get("system", {}).get("armed", True),
            "cameras": cameras,
            "total_events": total,
            "latest": latest_dict,
            "model_path": cfg.get("detection", {}).get("model_path", "—"),
            "schedule": _get_schedule_info(cfg),
        },
    )


@router.post("/arm", response_class=HTMLResponse)
async def arm(request: Request):
    config_store.set_armed(True)
    return _arm_badge(request, armed=True)


@router.post("/disarm", response_class=HTMLResponse)
async def disarm(request: Request):
    config_store.set_armed(False)
    return _arm_badge(request, armed=False)


def _arm_badge(request: Request, *, armed: bool) -> HTMLResponse:
    """Return just the status badge fragment for HTMX swap."""
    return templates.TemplateResponse(
        request,
        "partials/arm_badge.html",
        {"armed": armed},
    )
