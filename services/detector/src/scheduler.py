"""Arm/disarm scheduler — applies configured arm/disarm schedule to the armed flag.

Checks every 60 seconds and fires transitions at scheduled arm_time / disarm_time.
Manual arm/disarm via the UI is naturally respected: the scheduler only changes
state at scheduled transition boundaries, not continuously.

Note: the scheduler only fires at transition points — it does NOT snap the armed
state on startup or reconfigure to match the current window. If the system restarts
mid-window the armed state remains whatever was last written to config.
"""

import logging
import threading
from collections.abc import Callable
from datetime import date as dt_date
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 60  # seconds between schedule checks


class ArmScheduler:
    """Checks every 60 s and fires arm/disarm transitions at configured times."""

    def __init__(
        self,
        armed_ref: list[bool],
        on_transition: Callable[[bool], None],
    ) -> None:
        self._armed_ref = armed_ref
        self._on_transition = on_transition
        # All mutable config fields are protected by _cfg_lock so that
        # configure() (config-watcher thread) and _tick() (scheduler thread)
        # can run concurrently without seeing a half-updated config.
        self._cfg_lock = threading.Lock()
        self._tz_name = "UTC"
        self._arm_time: dt_time | None = None
        self._disarm_time: dt_time | None = None
        self._use_solar = False
        self._latitude: float | None = None
        self._longitude: float | None = None
        self._enabled = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_tick: datetime = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def configure(self, schedule_cfg: dict, tz_name: str = "UTC") -> None:
        """Apply (or re-apply on hot-reload) schedule config.

        Thread-safe: acquires _cfg_lock so concurrent _tick() calls see a
        consistent snapshot of all config fields.
        """
        use_solar = bool(schedule_cfg.get("use_solar", False))
        lat = schedule_cfg.get("latitude")
        lon = schedule_cfg.get("longitude")
        arm_str = schedule_cfg.get("arm_time") or ""
        disarm_str = schedule_cfg.get("disarm_time") or ""
        arm_t = _parse_time(arm_str)
        disarm_t = _parse_time(disarm_str)

        cfg_enabled = bool(schedule_cfg.get("enabled", False))
        if use_solar:
            enabled = cfg_enabled and lat is not None and lon is not None
        else:
            enabled = cfg_enabled and bool(arm_t and disarm_t)

        with self._cfg_lock:
            self._tz_name = tz_name
            self._use_solar = use_solar
            self._latitude = lat
            self._longitude = lon
            self._arm_time = arm_t
            self._disarm_time = disarm_t
            self._enabled = enabled

        if enabled:
            logger.info(
                "Schedule configured: arm=%s disarm=%s solar=%s tz=%s",
                arm_str or "sunrise",
                disarm_str or "sunset",
                use_solar,
                tz_name,
            )
        else:
            if not cfg_enabled:
                logger.info("Schedule disabled (enabled=false in config)")
            else:
                logger.info("Schedule disabled (no valid arm_time/disarm_time configured)")

    def get_next_transition(self) -> tuple[datetime, bool] | None:
        """Return (utc_time, target_armed) for the next scheduled transition, or None."""
        with self._cfg_lock:
            enabled = self._enabled
            get_arm = self._get_arm_time
            get_disarm = self._get_disarm_time
        if not enabled:
            return None
        return next_transition_after(datetime.now(timezone.utc), get_arm, get_disarm)

    @property
    def enabled(self) -> bool:
        with self._cfg_lock:
            return self._enabled

    def start(self) -> None:
        self._stop_event.clear()
        self._last_tick = datetime.now(timezone.utc)
        self._thread = threading.Thread(
            target=self._run, name="arm-scheduler", daemon=True
        )
        self._thread.start()
        logger.info("Arm scheduler started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Arm scheduler stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.wait(_CHECK_INTERVAL):
            try:
                self._tick()
            except Exception:
                logger.exception("Error in arm scheduler tick")

    def _tick(self) -> None:
        with self._cfg_lock:
            enabled = self._enabled
            get_arm = self._get_arm_time
            get_disarm = self._get_disarm_time
        if not enabled:
            return

        now = datetime.now(timezone.utc)
        last = self._last_tick
        self._last_tick = now

        for t_time, t_armed in transitions_between(last, now, get_arm, get_disarm):
            logger.info(
                "Scheduled transition at %s: armed → %s",
                t_time.strftime("%Y-%m-%d %H:%M %Z"),
                t_armed,
            )
            self._armed_ref[0] = t_armed
            try:
                self._on_transition(t_armed)
            except Exception:
                logger.exception("Error in arm/disarm transition callback")

    def _get_arm_time(self, d: dt_date) -> datetime | None:
        # Called with _cfg_lock held (or from within a locked context).
        if self._use_solar:
            return _sunrise(d, self._latitude, self._longitude)
        if self._arm_time:
            return _localtime_to_utc(d, self._arm_time, self._tz_name)
        return None

    def _get_disarm_time(self, d: dt_date) -> datetime | None:
        # Called with _cfg_lock held (or from within a locked context).
        if self._use_solar:
            return _sunset(d, self._latitude, self._longitude)
        if self._disarm_time:
            return _localtime_to_utc(d, self._disarm_time, self._tz_name)
        return None


# ------------------------------------------------------------------
# Module-level helpers (also imported by the web service for display)
# ------------------------------------------------------------------


def _parse_time(s: str) -> dt_time | None:
    if not s:
        return None
    try:
        parts = s.strip().split(":")
        return dt_time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        logger.warning("Invalid time format %r (expected HH:MM)", s)
        return None


def _localtime_to_utc(d: dt_date, t: dt_time, tz_name: str) -> datetime:
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        tz = ZoneInfo("UTC")
    return datetime.combine(d, t, tzinfo=tz).astimezone(timezone.utc)


def _sunrise(d: dt_date, lat: float | None, lon: float | None) -> datetime | None:
    if lat is None or lon is None:
        return None
    try:
        from astral import LocationInfo
        from astral.sun import sun as astral_sun

        loc = LocationInfo(latitude=lat, longitude=lon)
        s = astral_sun(loc.observer, date=d, tzinfo=timezone.utc)
        result: datetime = s["sunrise"]
        return result
    except ImportError:
        logger.warning("astral not installed — solar schedule unavailable; pip install astral")
        return None
    except Exception:
        logger.exception("Failed to compute sunrise for %s", d)
        return None


def _sunset(d: dt_date, lat: float | None, lon: float | None) -> datetime | None:
    if lat is None or lon is None:
        return None
    try:
        from astral import LocationInfo
        from astral.sun import sun as astral_sun

        loc = LocationInfo(latitude=lat, longitude=lon)
        s = astral_sun(loc.observer, date=d, tzinfo=timezone.utc)
        result: datetime = s["sunset"]
        return result
    except ImportError:
        logger.warning("astral not installed — solar schedule unavailable; pip install astral")
        return None
    except Exception:
        logger.exception("Failed to compute sunset for %s", d)
        return None


def transitions_between(
    start: datetime,
    end: datetime,
    get_arm_time: Callable[[dt_date], datetime | None],
    get_disarm_time: Callable[[dt_date], datetime | None],
) -> list[tuple[datetime, bool]]:
    """Return all arm/disarm transitions in the half-open interval (start, end]."""
    found: list[tuple[datetime, bool]] = []
    current_date = start.date()
    end_date = end.date()
    while current_date <= end_date:
        for target_armed, getter in [(True, get_arm_time), (False, get_disarm_time)]:
            t = getter(current_date)
            if t is not None and start < t <= end:
                found.append((t, target_armed))
        current_date += timedelta(days=1)
    found.sort(key=lambda x: x[0])
    return found


def next_transition_after(
    after: datetime,
    get_arm_time: Callable[[dt_date], datetime | None],
    get_disarm_time: Callable[[dt_date], datetime | None],
) -> tuple[datetime, bool] | None:
    """Return the next (utc_time, target_armed) transition after *after*, or None."""
    current_date = after.date()
    for _ in range(3):  # search up to 3 days ahead
        candidates: list[tuple[datetime, bool]] = []
        for target_armed, getter in [(True, get_arm_time), (False, get_disarm_time)]:
            t = getter(current_date)
            if t is not None and t > after:
                candidates.append((t, target_armed))
        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0]
        current_date += timedelta(days=1)
    return None
