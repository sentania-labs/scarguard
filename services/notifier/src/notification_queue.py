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
survive a notifier container restart. Every save writes a temporary file
next to the queue file, fsyncs it and renames it into place, so an
interruption (crash, SIGKILL at the end of the stop grace period, power
loss) leaves either the previous or the new queue on disk - never a
truncated one that would discard every pending retry on the next start.

Entries are processed per channel by :mod:`channel_dispatcher` (``due``,
``mark_delivered``, ``mark_failed``); :meth:`NotificationQueue.process_due`
keeps the one-shot form for callers that hold a plain list of notifiers.
"""

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import asdict, dataclass
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
    notifier_type: str        # channel name (new) or class name (legacy entries)
    attempt: int              # how many send attempts have been made so far
    next_retry: float         # Unix timestamp: earliest time to try again
    first_failed: float       # Unix timestamp: when this entry was first created


class NotificationQueue:
    """Thread-safe, disk-backed queue for failed notifications with retry."""

    def __init__(
        self,
        queue_path: str = _QUEUE_PATH,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(queue_path)
        self._tmp_path = self._path.with_name(self._path.name + ".tmp")
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: list[QueueEntry] = []
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load persisted queue from disk on startup, discarding expired entries."""
        try:
            # A leftover temporary file means a save was interrupted before the
            # rename; the queue file itself is still the last complete save.
            self._tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        if not self._path.exists():
            return
        try:
            raw: list[dict] = json.loads(self._path.read_text())
            now = self._clock()
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
                    "All %d persisted notification(s) expired during downtime - discarded",
                    expired,
                )
        except Exception:
            logger.exception(
                "Failed to load notification queue from %s; starting with empty queue",
                self._path,
            )
            self._entries = []

    def _save(self) -> None:
        """Write the current queue to disk atomically.  Must be called with self._lock held.

        The JSON is written to a temporary file in the same directory, flushed
        to disk and renamed over the queue file. ``os.replace`` is atomic on
        POSIX, so a reader (the next notifier start) sees either the previous
        complete queue or the new one.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps([asdict(e) for e in self._entries], indent=2)
            with open(self._tmp_path, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(self._tmp_path, self._path)
            self._fsync_dir()
        except Exception:
            logger.exception("Failed to persist notification queue to %s", self._path)
            try:
                self._tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _fsync_dir(self) -> None:
        """Best-effort: make the rename itself durable."""
        try:
            fd = os.open(self._path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    # ── Public interface ─────────────────────────────────────────────────────

    @property
    def depth(self) -> int:
        """Current number of entries waiting for retry."""
        with self._lock:
            return len(self._entries)

    def depth_by_type(self) -> dict[str, int]:
        """Number of pending entries per channel name."""
        with self._lock:
            counts: dict[str, int] = {}
            for entry in self._entries:
                counts[entry.notifier_type] = counts.get(entry.notifier_type, 0) + 1
            return counts

    def enqueue(self, event: dict, notifier: object) -> QueueEntry:
        """Add a failed notification to the retry queue and return its entry.

        If the queue is at capacity, the oldest entry is dropped to make room.
        Uses the notifier's channel name (via .name property) for retry matching;
        falls back to the class name for notifiers that don't expose a name.
        """
        notifier_type = getattr(notifier, "name", None) or type(notifier).__name__
        now = self._clock()
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
                    "Queue full (%d items) - dropped oldest entry: %s queued at %s",
                    _MAX_QUEUE_SIZE,
                    dropped.notifier_type,
                    datetime.fromtimestamp(dropped.first_failed, tz=timezone.utc).isoformat(),
                )
            self._entries.append(entry)
            queue_depth = len(self._entries)
            self._save()
        logger.info(
            "Queued failed %s notification for retry in %ds (queue depth: %d)",
            notifier_type,
            _BACKOFF_STEPS[0],
            queue_depth,
        )
        return entry

    def enqueue_many(self, events: list[dict], notifier: object) -> int:
        """Add several events for one notifier with a single save; returns the count added.

        Used when a channel persists its waiting events at shutdown, so the
        file is rewritten once rather than once per event inside the stop
        grace period. The capacity rule is the same as :meth:`enqueue`.
        """
        if not events:
            return 0
        notifier_type = getattr(notifier, "name", None) or type(notifier).__name__
        now = self._clock()
        dropped = 0
        with self._lock:
            for event in events:
                if len(self._entries) >= _MAX_QUEUE_SIZE:
                    self._entries.pop(0)
                    dropped += 1
                self._entries.append(QueueEntry(
                    event=event,
                    notifier_type=notifier_type,
                    attempt=0,
                    next_retry=now + _BACKOFF_STEPS[0],
                    first_failed=now,
                ))
            queue_depth = len(self._entries)
            self._save()
        if dropped:
            logger.warning(
                "Queue full (%d items) - dropped %d oldest entr%s to make room",
                _MAX_QUEUE_SIZE, dropped, "y" if dropped == 1 else "ies",
            )
        logger.info(
            "Queued %d %s notification(s) for retry in %ds (queue depth: %d)",
            len(events), notifier_type, _BACKOFF_STEPS[0], queue_depth,
        )
        return len(events)

    def due(self, types: Collection[str] | None = None) -> list[QueueEntry]:
        """Entries whose retry time has arrived, optionally limited to *types*.

        Returns a snapshot so callers never hold the queue lock during network
        I/O; report the outcome with :meth:`mark_delivered` / :meth:`mark_failed`.
        """
        now = self._clock()
        with self._lock:
            return [
                e for e in self._entries
                if e.next_retry <= now and (types is None or e.notifier_type in types)
            ]

    def _remove_locked(self, entry: QueueEntry) -> bool:
        """Remove *entry* (by identity).  Must be called with self._lock held."""
        for i, current in enumerate(self._entries):
            if current is entry:
                del self._entries[i]
                return True
        return False

    def mark_delivered(self, entry: QueueEntry) -> None:
        """A retry attempt for *entry* succeeded: remove it and persist."""
        now = self._clock()
        with self._lock:
            removed = self._remove_locked(entry)
            if removed:
                self._save()
        if removed:
            logger.info(
                "Queued %s notification delivered (attempt %d, %.1fh after initial failure)",
                entry.notifier_type,
                entry.attempt + 1,
                (now - entry.first_failed) / 3600,
            )

    def discard(self, entry: QueueEntry) -> bool:
        """Remove *entry* if it is still pending (e.g. the original send completed late)."""
        with self._lock:
            removed = self._remove_locked(entry)
            if removed:
                self._save()
        return removed

    def mark_failed(self, entry: QueueEntry) -> bool:
        """A retry attempt for *entry* failed: back off, or drop it once too old.

        Returns ``True`` when the entry was dropped for exceeding the retry window.
        """
        now = self._clock()
        with self._lock:
            if not any(current is entry for current in self._entries):
                # Already delivered late or dropped; nothing to back off.
                return False
            entry.attempt += 1
            elapsed = now - entry.first_failed
            dropped = elapsed >= _MAX_AGE_SECONDS
            if dropped:
                self._remove_locked(entry)
            else:
                delay = _BACKOFF_STEPS[min(entry.attempt, len(_BACKOFF_STEPS) - 1)]
                entry.next_retry = now + delay
            queue_depth = len(self._entries)
            self._save()
        if dropped:
            logger.warning(
                "Dropping %s notification after %.1fh and %d attempt(s) - "
                "max retry window exceeded",
                entry.notifier_type,
                elapsed / 3600,
                entry.attempt,
            )
        else:
            logger.info(
                "Retry %d for %s failed - next attempt in %ds "
                "(%.1fh elapsed, queue depth: %d)",
                entry.attempt,
                entry.notifier_type,
                delay,
                elapsed / 3600,
                queue_depth,
            )
        return dropped

    def process_due(
        self,
        notifiers: list,
        notifiers_lock: threading.Lock,
        types: Collection[str] | None = None,
    ) -> None:
        """Attempt to send any notifications whose retry time has arrived.

        One-shot form: sends are made inline on the calling thread. The
        channel workers in :mod:`channel_dispatcher` use :meth:`due` and the
        ``mark_*`` methods instead so one channel's retries never wait on
        another's.
        """
        due = self.due(types)
        if not due:
            return

        with notifiers_lock:
            active_notifiers = list(notifiers)
            # Index by channel name (.name property) first; fall back to class name
            # so legacy queue entries (stored by class name) still match.
            notifiers_by_type: dict[str, Any] = {}
            for notifier in active_notifiers:
                channel_name = getattr(notifier, "name", None) or type(notifier).__name__
                notifiers_by_type[channel_name] = notifier
                notifiers_by_type.setdefault(type(notifier).__name__, notifier)

        for entry in due:
            target = notifiers_by_type.get(entry.notifier_type)
            if target is None:
                # Notifier was disabled in config; leave entry in queue.
                logger.debug(
                    "Retry deferred - %s is not currently enabled", entry.notifier_type
                )
                continue
            try:
                target.send(entry.event)
            except Exception:
                self.mark_failed(entry)
            else:
                self.mark_delivered(entry)
