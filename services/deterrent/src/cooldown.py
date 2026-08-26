"""Cooldown trackers for actuation sequences.

Two layers:

- ``CooldownTracker`` — a single global gate protecting against repeat
  actuations of any kind inside ``defaults.cooldown_seconds``.  Prevents
  cross-group rapid-fire (e.g. camera A firing "minor" and camera B firing
  "thermonuclear" 50 ms apart).
- ``GroupCooldownTracker`` — per-group gate gating repeat firings of the
  same named group.  Lets you run a long group cooldown (10 min for
  "thermonuclear") on top of a short global one (30 s).
"""

from __future__ import annotations

import threading
import time


class CooldownTracker:
    """Thread-safe global cooldown gate."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # ``None`` means "never actuated".  Do NOT use 0.0 as the sentinel:
        # ``time.monotonic()`` counts from system boot, so on a host whose
        # uptime is still below the cooldown a 0.0 sentinel reads as "fired
        # at boot" and the gate stays shut.  That suppressed deterrents for
        # the first ``cooldown_seconds`` after every reboot.
        self._last_actuation: float | None = None

    def is_clear(self, cooldown_seconds: int) -> bool:
        """Return ``True`` if enough time has passed since the last actuation."""
        with self._lock:
            if self._last_actuation is None:
                return True
            return (time.monotonic() - self._last_actuation) >= cooldown_seconds

    def record(self) -> None:
        """Record that an actuation just completed."""
        with self._lock:
            self._last_actuation = time.monotonic()

    def seconds_remaining(self, cooldown_seconds: int) -> float:
        """Return how many seconds remain before cooldown expires (0 if clear)."""
        with self._lock:
            if self._last_actuation is None:
                return 0.0
            remaining = cooldown_seconds - (time.monotonic() - self._last_actuation)
            return max(0.0, remaining)


class GroupCooldownTracker:
    """Per-group cooldown tracker.

    Each group's last-fired monotonic timestamp lives in a dict guarded by
    a single lock.  A group absent from the dict has never fired; absence is
    the sentinel, for the same boot-clock reason described above.
    ``cooldown_seconds`` is passed per-query so a group's cooldown can be
    updated from config without rebuilding the tracker.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def is_clear(self, group_name: str, cooldown_seconds: int) -> bool:
        with self._lock:
            last = self._last.get(group_name)
            if last is None:
                return True
            return (time.monotonic() - last) >= cooldown_seconds

    def record(self, group_name: str) -> None:
        with self._lock:
            self._last[group_name] = time.monotonic()

    def seconds_remaining(self, group_name: str, cooldown_seconds: int) -> float:
        with self._lock:
            last = self._last.get(group_name)
            if last is None:
                return 0.0
            remaining = cooldown_seconds - (time.monotonic() - last)
            return max(0.0, remaining)

    def forget(self, group_name: str) -> None:
        """Remove tracking for a group (e.g. after it's deleted from config)."""
        with self._lock:
            self._last.pop(group_name, None)
