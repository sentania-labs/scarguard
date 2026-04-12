"""Global cooldown tracker for actuation sequences."""

from __future__ import annotations

import threading
import time


class CooldownTracker:
    """Thread-safe cooldown gate.

    MVP: single global cooldown.  Per-species cooldowns are planned for
    0.13.x when response profiles are added.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_actuation: float = 0.0

    def is_clear(self, cooldown_seconds: int) -> bool:
        """Return ``True`` if enough time has passed since the last actuation."""
        with self._lock:
            return (time.monotonic() - self._last_actuation) >= cooldown_seconds

    def record(self) -> None:
        """Record that an actuation just completed."""
        with self._lock:
            self._last_actuation = time.monotonic()

    def seconds_remaining(self, cooldown_seconds: int) -> float:
        """Return how many seconds remain before cooldown expires (0 if clear)."""
        with self._lock:
            remaining = cooldown_seconds - (time.monotonic() - self._last_actuation)
            return max(0.0, remaining)
