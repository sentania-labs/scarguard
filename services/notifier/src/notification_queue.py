"""Disk-backed notification retry queue with incremental exponential backoff.

Failed notifications are queued and retried with the following schedule:
  attempt 1 → wait 30 s
  attempt 2 → wait 60 s
  attempt 3 → wait 120 s
  attempt 4 → wait 240 s
  attempt 5 → wait 480 s
  attempt 6+ → wait 600 s (10-minute cap)

Notifications that remain undelivered for more than 24 hours are discarded.
The queue is persisted to a JSON file on the data volume so that entries
survive a notifier container restart.
"""

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Backoff schedule in seconds.  The last entry is the cap.
_BACKOFF_STEPS: list[int] = [30, 60, 120, 240, 480, 600]
# Maximum time (seconds) to keep retrying before discarding (24 h).
_MAX_AGE_SECONDS: int = 86_400
# Upper bound on queue length; oldest entry is dropped when full.
_MAX_QUEUE_SIZE: int = 500
# How often the background worker wakes up to process due retries (seconds).
WORKER_INTERVAL: int = 15

_QUEUE_PATH: str = os.environ.get(
    "QUEUE_PATH",
    os.path.join(
        os.environ.get("NOTIFIER_STATE_DIR", "/var/lib/scarguard"),
        "notification_queue.json",
    ),
)


@dataclass
class QueueEntry:
    event: dict[str, Any]
    notifier_type: str        # class name, e.g. "DiscordNotifier"
    attempt: int              # how many send attempts have been made so far
    next_retry: float         # Unix timestamp: earliest time to try again
    first_failed: float       # Unix timestamp: when this entry was first created


class NotificationQueue:
    """Thread-safe, disk-backed queue for failed notifications with retry."""

    def __init__(self, queue_path: str = _QUEUE_PATH) -> None:
        self._path = Path(queue_path)
        self._lock = threading.Lock()
        self._entries: list[QueueEntry] = []
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load persisted queue from disk on startup, discarding expired entries."""
        if not self._path.exists():
            return
        try:
            raw: list[dict] = json.loads(self._path.read_text())
            now = time.time()
            kept = [
                QueueEntry(**item)
                for item in raw
                if now - item.get("first_failed", 0) <= _MAX_AGE_SECONDS
            ]
            self._entries = kept
            expired = len(raw) - len(kept)
            if kept:
                logger.info(
                    "Loaded %d queued notification(s) from disk (%d expired, discarded)",
                    len(kept),
                    expired,
                )
            elif expired:
                logger.info(
                    "All %d persisted notification(s) expired during downtime — discarded",
                    expired,
                )
        except Exception:
            logger.exception(
                "Failed to load notification queue from %s; starting with empty queue",
                self._path,
            )
            self._entries = []

    def _save(self) -> None:
        """Write the current queue to disk.  Must be called with self._lock held."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps([asdict(e) for e in self._entries], indent=2)
            )
        except Exception:
            logger.exception("Failed to persist notification queue to %s", self._path)

    # ── Public interface ─────────────────────────────────────────────────────

    @property
    def depth(self) -> int:
        """Current number of entries waiting for retry."""
        with self._lock:
            return len(self._entries)

    def enqueue(self, event: dict, notifier: object) -> None:
        """Add a failed notification to the retry queue.

        If the queue is at capacity, the oldest entry is dropped to make room.
        """
        notifier_type = type(notifier).__name__
        now = time.time()
        entry = QueueEntry(
            event=event,
            notifier_type=notifier_type,
            attempt=0,
            next_retry=now + _BACKOFF_STEPS[0],
            first_failed=now,
        )
        with self._lock:
            if len(self._entries) >= _MAX_QUEUE_SIZE:
                dropped = self._entries.pop(0)
                logger.warning(
                    "Queue full (%d items) — dropped oldest entry: %s queued at %s",
                    _MAX_QUEUE_SIZE,
                    dropped.notifier_type,
                    datetime.fromtimestamp(dropped.first_failed, tz=timezone.utc).isoformat(),
                )
            self._entries.append(entry)
            self._save()
        logger.info(
            "Queued failed %s notification for retry in %ds (queue depth: %d)",
            notifier_type,
            _BACKOFF_STEPS[0],
            self.depth,
        )

    def process_due(self, notifiers: list, notifiers_lock: threading.Lock) -> None:
        """Attempt to send any notifications whose retry time has arrived.

        Called periodically by the background worker thread.
        """
        now = time.time()

        # Snapshot entries that are due — don't hold the lock during network I/O.
        with self._lock:
            due = [e for e in self._entries if e.next_retry <= now]

        if not due:
            return

        with notifiers_lock:
            active_notifiers = list(notifiers)

        to_remove: list[QueueEntry] = []

        for entry in due:
            target = next(
                (n for n in active_notifiers if type(n).__name__ == entry.notifier_type),
                None,
            )
            if target is None:
                # Notifier was disabled in config; leave entry in queue.
                logger.debug(
                    "Retry deferred — %s is not currently enabled", entry.notifier_type
                )
                continue

            try:
                target.send(entry.event)
                elapsed_h = (now - entry.first_failed) / 3600
                logger.info(
                    "Queued %s notification delivered (attempt %d, %.1fh after initial failure)",
                    entry.notifier_type,
                    entry.attempt + 1,
                    elapsed_h,
                )
                to_remove.append(entry)

            except Exception:
                entry.attempt += 1
                elapsed = now - entry.first_failed
                if elapsed >= _MAX_AGE_SECONDS:
                    logger.warning(
                        "Dropping %s notification after %.1fh and %d attempt(s) — "
                        "max retry window exceeded",
                        entry.notifier_type,
                        elapsed / 3600,
                        entry.attempt,
                    )
                    to_remove.append(entry)
                else:
                    delay = _BACKOFF_STEPS[min(entry.attempt, len(_BACKOFF_STEPS) - 1)]
                    entry.next_retry = now + delay
                    logger.info(
                        "Retry %d for %s failed — next attempt in %ds "
                        "(%.1fh elapsed, queue depth: %d)",
                        entry.attempt,
                        entry.notifier_type,
                        delay,
                        elapsed / 3600,
                        self.depth,
                    )

        # Commit changes (removals + updated next_retry timestamps) to disk.
        if to_remove or due:
            with self._lock:
                for entry in to_remove:
                    try:
                        self._entries.remove(entry)
                    except ValueError:
                        pass
                self._save()
