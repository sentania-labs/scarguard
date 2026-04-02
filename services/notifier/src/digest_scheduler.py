"""Digest scheduler — fires periodic digest report generation and dispatch."""

import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 60  # seconds


class DigestScheduler:
    """Daemon thread that triggers digest report generation at the configured time.

    Checks every 60 seconds whether the current time (in the configured timezone)
    has passed the scheduled time and the digest hasn't been sent today.

    Hot-reloadable: call ``configure()`` with new settings at any time.
    """

    def __init__(
        self,
        dispatch_fn: Callable[..., Any],
        notifiers: list,
        notifiers_lock: threading.Lock,
    ) -> None:
        self._dispatch_fn = dispatch_fn
        self._notifiers = notifiers
        self._notifiers_lock = notifiers_lock

        # Config (set via configure())
        self._enabled = False
        self._frequency = "daily"
        self._time_str = "07:00"
        self._channels: list[str] = []
        self._tz_name = "UTC"

        self._last_sent_date: date | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="digest-scheduler", daemon=True
        )

    def configure(self, report_cfg: dict, tz_name: str = "UTC") -> None:
        """Update schedule configuration (hot-reloadable)."""
        with self._lock:
            self._enabled = bool(report_cfg.get("enabled", False))
            self._frequency = report_cfg.get("frequency", "daily")
            self._time_str = report_cfg.get("time", "07:00")
            self._channels = list(report_cfg.get("channels", []))
            self._tz_name = tz_name

        if self._enabled and self._channels:
            logger.info(
                "Digest scheduler configured: %s at %s → %s",
                self._frequency, self._time_str, ", ".join(self._channels),
            )
        elif self._enabled:
            logger.warning("Digest enabled but no channels configured — digest will not send")
        else:
            logger.info("Digest scheduler disabled")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(timeout=_CHECK_INTERVAL):
            self._tick()

    def _tick(self) -> None:
        with self._lock:
            if not self._enabled or not self._channels:
                return
            frequency = self._frequency
            time_str = self._time_str
            channels = list(self._channels)
            tz_name = self._tz_name

        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, KeyError):
            tz = ZoneInfo("UTC")

        now = datetime.now(tz)
        today = now.date()

        # Parse scheduled time
        try:
            parts = time_str.split(":")
            scheduled = time(int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            scheduled = time(7, 0)

        # Already sent today?
        if self._last_sent_date == today:
            return

        # Check frequency: daily=every day, weekly=Mondays, monthly=1st
        if frequency == "weekly" and today.weekday() != 0:
            return
        if frequency == "monthly" and today.day != 1:
            return

        # Has the scheduled time passed?
        if now.time() < scheduled:
            return

        # Fire — only mark as sent if dispatch succeeds
        if self._send_digest(frequency, channels):
            self._last_sent_date = today

    def _send_digest(self, frequency: str, channels: list[str]) -> bool:
        """Generate and dispatch the digest report. Returns True on success."""
        try:
            import digest

            report = digest.generate(frequency)
            # Route through the normal dispatch pipeline with channel targeting
            report["actions_triggered"] = channels
            report["class_name"] = "digest"
            report["confidence"] = 1.0
            report["camera_name"] = "_system"
            report["timestamp"] = report["generated_at"]
            report["snapshot_path"] = None

            self._dispatch_fn(
                report, self._notifiers, self._notifiers_lock, None
            )
            logger.info("Digest report dispatched to: %s", ", ".join(channels))
            return True
        except Exception:
            logger.exception("Failed to generate/dispatch digest report — will retry next tick")
            return False
