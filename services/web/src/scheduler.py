import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone, tzinfo
from datetime import time as dt_time
from zoneinfo import ZoneInfo

import config_store

logger = logging.getLogger(__name__)
_CHECK_INTERVAL = 30.0  # seconds


def _parse_time(s: str) -> dt_time | None:
    if not s:
        return None
    try:
        parts = s.split(":")
        return dt_time(int(parts[0]), int(parts[1]))
    except Exception:
        return None


def _localtime_to_utc(d, t: dt_time, tz: tzinfo) -> datetime:
    """Convert a local wall-clock date/time to UTC."""
    return datetime.combine(d, t, tzinfo=tz).astimezone(timezone.utc)


def next_occurrence(t: dt_time, after: datetime, tz: tzinfo) -> datetime:
    """Return the next occurrence of *t* after *after*, localised to *tz*."""
    candidate = _localtime_to_utc(after.date(), t, tz)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def _compute_solar_transitions(
    now_utc: datetime,
    latitude: float | None,
    longitude: float | None,
    tz: tzinfo,
) -> tuple[datetime | None, datetime | None]:
    """Compute today/tomorrow sunrise and sunset in UTC.

    Returns (sunrise_utc, sunset_utc) or (None, None) on failure.
    """
    if latitude is None or longitude is None:
        return None, None

    try:
        from astral import LocationInfo
        from astral.sun import sun as astral_sun
    except ImportError:
        logger.warning("astral not available for solar schedule")
        return None, None

    try:
        loc = LocationInfo(latitude=latitude, longitude=longitude)
        observer = loc.observer
    except Exception:
        logger.warning("Failed to create solar location for %s, %s", latitude, longitude)
        return None, None

    # Check today and tomorrow for transitions within the next day+
    now_date = now_utc.astimezone(tz).date()
    for offset in range(3):
        d = now_date + timedelta(days=offset)
        try:
            s = astral_sun(observer, date=d, tzinfo=tz)
        except Exception as exc:
            logger.warning("Failed to compute solar times for %s: %s", d, exc)
            continue

        sunrise = s.get("sunrise")
        sunset = s.get("sunset")

        # Convert to UTC for comparison
        if sunrise is not None and sunrise.tzinfo is None:
            sunrise = sunrise.replace(tzinfo=tz)
        if sunset is not None and sunset.tzinfo is None:
            sunset = sunset.replace(tzinfo=tz)

        if sunrise is not None and sunrise.tzinfo is not None:
            sunrise_utc = sunrise.astimezone(timezone.utc)
        else:
            sunrise_utc = None
        if sunset is not None and sunset.tzinfo is not None:
            sunset_utc = sunset.astimezone(timezone.utc)
        else:
            sunset_utc = None

        return sunrise_utc, sunset_utc

    return None, None


def transitions_between(
    start: datetime,
    end: datetime,
    get_arm_time: Callable[[datetime], dt_time | None],
    get_disarm_time: Callable[[datetime], dt_time | None],
    tz: tzinfo,
) -> list[tuple[datetime, bool]]:
    transitions = []

    arm_t = get_arm_time(start)
    if arm_t:
        t = next_occurrence(arm_t, start, tz)
        if t <= end:
            transitions.append((t, True))

    disarm_t = get_disarm_time(start)
    if disarm_t:
        t = next_occurrence(disarm_t, start, tz)
        if t <= end:
            transitions.append((t, False))

    transitions.sort()
    return transitions

class ArmScheduler:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_tick = datetime.now(timezone.utc)
        self._tz: tzinfo = timezone.utc

    def start(self) -> None:
        self._stop_event.clear()
        self._last_tick = datetime.now(timezone.utc)
        self._thread = threading.Thread(
            target=self._run, name="arm-scheduler", daemon=True
        )
        self._thread.start()
        logger.info("Web Arm scheduler started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Web Arm scheduler stopped")

    def _run(self) -> None:
        while not self._stop_event.wait(_CHECK_INTERVAL):
            try:
                self._tick()
            except Exception:
                logger.exception("Error in arm scheduler tick")

    def _get_arm_time(self, dt: datetime) -> dt_time | None:
        cfg = config_store.load()
        sched = cfg.get("system", {}).get("schedule", {})
        if sched.get("use_solar"):
            # Solar mode: return a sentinel dt_time at midnight to signal the
            # caller that solar transitions should be used instead of a fixed
            # wall-clock time.  _tick() intercepts this and computes actual
            # sunrise/sunset.
            return dt_time(0, 0)  # sentinel: solar mode
        return _parse_time(sched.get("arm_time", ""))

    def _get_disarm_time(self, dt: datetime) -> dt_time | None:
        cfg = config_store.load()
        sched = cfg.get("system", {}).get("schedule", {})
        if sched.get("use_solar"):
            return dt_time(0, 0)  # sentinel: solar mode
        return _parse_time(sched.get("disarm_time", ""))

    def _get_solar_transitions(self, start: datetime, end: datetime) -> list[tuple[datetime, bool]]:
        """Compute armed/disarmed transitions from solar data."""
        cfg = config_store.load()
        sched = cfg.get("system", {}).get("schedule", {})
        latitude = sched.get("latitude")
        longitude = sched.get("longitude")
        tz = self._tz

        if latitude is None or longitude is None:
            return []

        sunrise_utc, sunset_utc = _compute_solar_transitions(
            start, latitude, longitude, tz
        )
        transitions: list[tuple[datetime, bool]] = []

        if sunrise_utc is not None and start <= sunrise_utc <= end:
            transitions.append((sunrise_utc, True))
        if sunset_utc is not None and start <= sunset_utc <= end:
            transitions.append((sunset_utc, False))

        transitions.sort()
        return transitions

    def _schedule_allows_rearm(self) -> bool:
        now = datetime.now(timezone.utc)
        arm_t = self._get_arm_time(now)
        disarm_t = self._get_disarm_time(now)
        if not arm_t or not disarm_t:
            return True
        tz = self._tz
        local_now = now.astimezone(tz)
        local_time = local_now.time()
        if arm_t < disarm_t:
            return arm_t <= local_time < disarm_t
        else:
            return local_time >= arm_t or local_time < disarm_t

    def _tick(self) -> None:
        cfg = config_store.load()
        sys_cfg = cfg.get("system", {})
        sched = sys_cfg.get("schedule", {})
        enabled = sched.get("enabled", False)
        use_solar = sched.get("use_solar", False)

        try:
            tz_str = sys_cfg.get("timezone", "UTC")
            self._tz = ZoneInfo(tz_str)
        except Exception:
            self._tz = timezone.utc

        now = datetime.now(timezone.utc)
        last = self._last_tick
        self._last_tick = now

        if enabled:
            if use_solar:
                solar_transitions = self._get_solar_transitions(last, now)
                for t_time, t_armed in solar_transitions:
                    logger.info("Solar transition at %s: armed → %s", t_time, t_armed)
                    cfg = config_store.load()
                    cfg.setdefault("system", {})["armed"] = t_armed
                    config_store.save(cfg)
            else:
                for t_time, t_armed in transitions_between(
                    last, now, self._get_arm_time, self._get_disarm_time, self._tz
                ):
                    logger.info("Scheduled transition at %s: armed → %s", t_time, t_armed)
                    cfg = config_store.load()
                    cfg.setdefault("system", {})["armed"] = t_armed
                    config_store.save(cfg)

        self._check_pending_rearm()

    def _check_pending_rearm(self) -> None:
        cfg = config_store.load()
        auth_cfg = cfg.get("system", {}).get("auth", {})
        val = auth_cfg.get("rearm_at")
        if not val:
            return

        try:
            parsed = datetime.fromisoformat(val)
            if parsed.tzinfo is None:
                rearm_time = parsed.replace(tzinfo=timezone.utc)
            else:
                rearm_time = parsed.astimezone(timezone.utc)

            if datetime.now(timezone.utc) < rearm_time:
                return

            # Clear rearm_at
            cfg = config_store.load()
            auth_cfg = cfg.setdefault("system", {}).setdefault("auth", {})
            if "rearm_at" in auth_cfg:
                del auth_cfg["rearm_at"]

            if not self._schedule_allows_rearm():
                logger.info("Auto-rearm suppressed: schedule currently dictates disarmed")
                config_store.save(cfg)
                return

            logger.info("Non-admin auto-rearm triggered")
            cfg.setdefault("system", {})["armed"] = True
            config_store.save(cfg)

            import audit
            import auth

            db = auth.get_db(auth.AUTH_DB_PATH)
            try:
                audit.record(
                    db,
                    action="rearm.auto",
                    username="system",
                    details="Auto-rearm triggered by scheduler expiration"
                )
            finally:
                db.close()
        except Exception:
            logger.debug("Failed to check pending rearm", exc_info=True)
