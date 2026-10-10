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

def next_occurrence(t: dt_time, after: datetime) -> datetime:
    tz = after.tzinfo
    candidate = datetime.combine(after.date(), t, tzinfo=tz)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate

def transitions_between(
    start: datetime,
    end: datetime,
    get_arm_time: Callable[[datetime], dt_time | None],
    get_disarm_time: Callable[[datetime], dt_time | None],
) -> list[tuple[datetime, bool]]:
    transitions = []

    arm_t = get_arm_time(start)
    if arm_t:
        t = next_occurrence(arm_t, start)
        if t <= end:
            transitions.append((t, True))

    disarm_t = get_disarm_time(start)
    if disarm_t:
        t = next_occurrence(disarm_t, start)
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
        return _parse_time(sched.get("arm_time", ""))

    def _get_disarm_time(self, dt: datetime) -> dt_time | None:
        cfg = config_store.load()
        sched = cfg.get("system", {}).get("schedule", {})
        return _parse_time(sched.get("disarm_time", ""))

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

        try:
            tz_str = sys_cfg.get("timezone", "UTC")
            self._tz = ZoneInfo(tz_str)
        except Exception:
            self._tz = timezone.utc

        now = datetime.now(timezone.utc)
        last = self._last_tick
        self._last_tick = now

        if enabled:
            for t_time, t_armed in transitions_between(last, now, self._get_arm_time, self._get_disarm_time):
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
