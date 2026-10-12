"""Per-channel bounded delivery queues (FDY-0572, SG-33).

Before this module every notification was sent inline on the Redis
subscriber thread, one channel after another, and the retry worker handled
every channel's retries in one loop. A relay that accepted the TCP
connection and then said nothing held the subscriber - and therefore
Discord, ntfy and every other channel - for the whole SMTP conversation.

Now each enabled channel owns a :class:`ChannelWorker`: a bounded in-memory
queue drained by its own thread, which also processes that channel's
entries in the disk-backed retry queue. The subscriber only ever enqueues,
so a stalled channel can never delay another one or the subscriber itself.

Bounds and outcomes (every outcome is logged with the channel name and a
running counter, and exposed through :meth:`ChannelDispatcher.snapshot`):

* **Bounded queue** - a channel holds at most ``max_pending`` undelivered
  events. A further event is not dropped: it is written straight to the
  retry queue (itself capped at ``_MAX_QUEUE_SIZE`` with drop-oldest) and
  counted as ``overflowed``.
* **Finite deadline** - a delivery attempt runs in a short-lived thread and
  is given ``send_deadline`` seconds. Past that the event is queued for
  retry (``timed_out``) and the channel is *stalled* until the attempt ends
  on its own (the senders' socket timeouts guarantee it does). While
  stalled, new events for that channel go to the retry queue without an
  attempt (``deferred``) so sends never pile up. Should the late attempt
  still succeed, its retry entry is cancelled; a duplicate is possible only
  if the retry already fired, which is preferred to a lost alert.
* **Safe shutdown** - :meth:`ChannelDispatcher.stop` persists every pending
  event to the retry queue immediately, waits a bounded grace for in-flight
  attempts and persists those too, so a restart delivers them.

Tunables live in ``scarguard.yml`` under ``notifications.delivery``
(``queue_size``, ``send_deadline_seconds``), edited on the config page's
Notifications tab and applied on every config reload; see
:func:`delivery_settings`.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from notification_queue import WORKER_INTERVAL, NotificationQueue, QueueEntry

logger = logging.getLogger(__name__)


# Undelivered events a channel may hold before further ones spill to the retry queue.
DEFAULT_QUEUE_SIZE = 50
QUEUE_SIZE_RANGE = (1, 1000)
# Longest a single delivery attempt may run before it counts as failed (seconds).
DEFAULT_SEND_DEADLINE = 60
SEND_DEADLINE_RANGE = (5, 600)
# Time allowed at shutdown for in-flight attempts to finish before they are persisted.
STOP_GRACE: float = 5.0


def _bounded_int(delivery: dict, key: str, default: int, bounds: tuple[int, int]) -> int:
    raw = delivery.get(key, default)
    low, high = bounds
    if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
        logger.warning(
            "notifications.delivery.%s=%r is not a whole number between %d and %d - using %d",
            key, raw, low, high, default,
        )
        return default
    return raw


def delivery_settings(notif_cfg: Any) -> tuple[int, float]:
    """``(queue_size, send_deadline)`` from the ``notifications`` config section.

    The web config model enforces the same ranges on save; a hand-edited
    out-of-range or non-numeric value falls back to the default (logged).
    """
    delivery = notif_cfg.get("delivery") if isinstance(notif_cfg, dict) else None
    if not isinstance(delivery, dict):
        delivery = {}
    queue_size = _bounded_int(delivery, "queue_size", DEFAULT_QUEUE_SIZE, QUEUE_SIZE_RANGE)
    deadline = _bounded_int(delivery, "send_deadline_seconds", DEFAULT_SEND_DEADLINE, SEND_DEADLINE_RANGE)
    return queue_size, float(deadline)


@dataclass
class ChannelStats:
    """Running counters for one channel; every field is a delivery outcome."""

    submitted: int = 0    # events handed to the channel
    delivered: int = 0    # sends that completed (including late ones)
    failed: int = 0       # sends that raised - queued for retry
    timed_out: int = 0    # sends that exceeded the deadline - queued for retry
    deferred: int = 0     # events queued for retry without a send: channel stalled
    overflowed: int = 0   # events queued for retry without a send: queue full
    retried: int = 0      # retry-queue entries attempted by this channel
    persisted: int = 0    # events written to the retry queue at shutdown/retire


class _Attempt:
    """One delivery attempt, run on its own thread so it can be given a deadline.

    The outcome is settled exactly once under ``lock``: either the attempt
    completes (success removes a retry entry it may have been given;
    failure queues one unless it already has one) or a waiter gives up on it
    via :meth:`ensure_retry`, which queues the event for retry so nothing is
    lost if the attempt never returns before the process ends. ``done`` is
    set only after the completed attempt's outcome has been recorded.
    """

    def __init__(self, worker: ChannelWorker, event: dict[str, Any], entry: QueueEntry | None) -> None:
        self.worker = worker
        self.notifier = worker.notifier
        self.event = event
        self.entry = entry            # retry-queue entry when this is a retry attempt
        self.retry_entry: QueueEntry | None = None
        self.error: BaseException | None = None
        self.done = False
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.thread = threading.Thread(
            target=self._run, name=f"notif-send-{worker.name}", daemon=True,
        )

    def _run(self) -> None:
        try:
            self.notifier.send(self.event)
        except Exception as exc:  # any sender failure means retry
            self.error = exc
        elapsed = time.monotonic() - self.started
        # Settle under the lock and only then mark the attempt done: until the
        # outcome is recorded, ensure_retry() (shutdown's persist_inflight)
        # either queues the event itself or waits for the settlement, so a
        # failure finishing during the stop grace is never lost on exit.
        with self.lock:
            try:
                self.worker._settle(self, self.retry_entry, elapsed)
            except Exception:
                logger.exception("[%s] failed to record delivery outcome", self.worker.name)
            finally:
                self.done = True
        self.worker._release(self)

    def ensure_retry(self, reason: str) -> bool:
        """Queue the event for retry unless the attempt finished or already has an entry."""
        with self.lock:
            if self.done or self.retry_entry is not None:
                return False
            if self.entry is None:
                self.retry_entry = self.worker.retry_queue.enqueue(self.event, self.notifier)
            else:
                self.worker.retry_queue.mark_failed(self.entry)
                self.retry_entry = self.entry
        logger.warning(
            "[%s] %s - event queued for retry",
            self.worker.name, reason,
        )
        return True


class ChannelWorker:
    """Bounded queue plus delivery thread for one notification channel."""

    def __init__(
        self,
        name: str,
        notifier: Any,
        retry_queue: NotificationQueue,
        *,
        retry_types: frozenset[str],
        max_pending: int,
        send_deadline: float,
        retry_interval: float,
    ) -> None:
        self.name = name
        self.notifier = notifier              # swapped in place on config reload
        self.retry_queue = retry_queue
        self.retry_types = retry_types        # retry-queue notifier_type values this channel owns
        self.stats = ChannelStats()
        self._pending: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_pending)
        self._deadline = send_deadline
        self._retry_interval = retry_interval
        self._stop = threading.Event()
        self._stats_lock = threading.Lock()
        self._current: _Attempt | None = None     # attempt the worker is waiting on / abandoned
        self._thread = threading.Thread(target=self._run, name=f"notif-{name}", daemon=True)

    # ── Observability ────────────────────────────────────────────────────────

    def _bump(self, field: str) -> int:
        with self._stats_lock:
            value = getattr(self.stats, field) + 1
            setattr(self.stats, field, value)
            return value

    @property
    def depth(self) -> int:
        """Events waiting in this channel's live queue."""
        return self._pending.qsize()

    @property
    def stalled(self) -> bool:
        """True while an attempt that exceeded its deadline is still running."""
        current = self._current
        return current is not None and current.retry_entry is not None and not current.done

    def snapshot(self) -> dict[str, Any]:
        with self._stats_lock:
            data = asdict(self.stats)
        data["pending"] = self.depth
        data["stalled"] = self.stalled
        return data

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: float) -> None:
        self._thread.join(max(0.0, timeout))

    def configure(self, max_pending: int, send_deadline: float) -> None:
        """Apply new bounds (config reload). Events already queued stay queued;
        a smaller queue only refuses new events until it drains below the bound."""
        with self._pending.mutex:
            self._pending.maxsize = max_pending
        self._deadline = send_deadline

    def submit(self, event: dict[str, Any]) -> str:
        """Hand *event* to the channel without blocking.

        Returns ``"queued"``, ``"overflow"`` (queue full, written to the retry
        queue) or ``"stopped"`` (shutting down, written to the retry queue).
        """
        self._bump("submitted")
        if self._stop.is_set():
            self.retry_queue.enqueue(event, self.notifier)
            self._bump("persisted")
            return "stopped"
        try:
            self._pending.put_nowait(event)
        except queue.Full:
            count = self._bump("overflowed")
            self.retry_queue.enqueue(event, self.notifier)
            logger.warning(
                "[%s] delivery queue full (%d pending) - event moved to retry queue "
                "(overflowed=%d, retry depth=%d)",
                self.name, self._pending.maxsize, count, self.retry_queue.depth,
            )
            return "overflow"
        if self._stop.is_set():
            # stop() may have drained between the check above and the put:
            # drain again so this event is persisted too.
            self._drain()
            return "stopped"
        return "queued"

    def _drain(self) -> None:
        """Move everything waiting in the live queue to the retry queue (one save)."""
        events: list[dict[str, Any]] = []
        while True:
            try:
                events.append(self._pending.get_nowait())
            except queue.Empty:
                break
        if not events:
            return
        self.retry_queue.enqueue_many(events, self.notifier)
        with self._stats_lock:
            self.stats.persisted += len(events)
        logger.info(
            "[%s] persisted %d pending notification(s) to the retry queue for delivery after restart",
            self.name, len(events),
        )

    def stop(self) -> None:
        """Stop accepting work and persist every pending event to the retry queue."""
        self._stop.set()
        self._drain()

    def persist_inflight(self) -> bool:
        """After the stop grace: queue the attempt still running so a restart delivers it."""
        current = self._current
        if current is None:
            return False
        if current.ensure_retry("shutdown reached while a delivery attempt was still running"):
            self._bump("persisted")
            return True
        return False

    # ── Delivery ─────────────────────────────────────────────────────────────

    def _run(self) -> None:
        logger.info(
            "[%s] delivery worker started (queue=%d, deadline=%.0fs, retry interval=%.0fs)",
            self.name, self._pending.maxsize, self._deadline, self._retry_interval,
        )
        next_retry_check = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_retry_check:
                if self._process_retries():
                    next_retry_check = time.monotonic() + self._retry_interval
                # else: a live event is waiting - deliver it, then resume retries.
            wait = max(0.05, min(1.0, next_retry_check - time.monotonic()))
            try:
                event = self._pending.get(timeout=wait)
            except queue.Empty:
                continue
            self._attempt(event, None)
        logger.info("[%s] delivery worker stopped", self.name)

    def _process_retries(self) -> bool:
        """Attempt this channel's due retry entries; live events take priority.

        Returns ``False`` when it stopped early because a live event arrived
        (after a restart every persisted entry is due at once, and a fresh
        alert must not wait behind that backlog).
        """
        for entry in self.retry_queue.due(self.retry_types):
            if self._stop.is_set():
                return True
            if not self._pending.empty():
                return False
            self._bump("retried")
            self._attempt(entry.event, entry)
        return True

    def _attempt(self, event: dict[str, Any], entry: QueueEntry | None) -> None:
        current = self._current
        if current is not None and not current.done:
            # The previous attempt exceeded its deadline and is still running:
            # do not stack another connection on a stalled relay.
            count = self._bump("deferred")
            if entry is None:
                self.retry_queue.enqueue(event, self.notifier)
            else:
                self.retry_queue.mark_failed(entry)
            logger.warning(
                "[%s] channel stalled by an attempt running %.0fs - event queued for retry "
                "without a send (deferred=%d)",
                self.name, time.monotonic() - current.started, count,
            )
            return

        attempt = _Attempt(self, event, entry)
        self._current = attempt
        if self._stop.is_set():
            # Shutdown began while this event was being taken off the queue:
            # persist it rather than start a send nobody will wait for.
            if attempt.ensure_retry("shutdown reached before the delivery attempt started"):
                self._bump("persisted")
            self._current = None
            return
        attempt.thread.start()
        attempt.thread.join(self._deadline)
        if attempt.ensure_retry(f"delivery attempt exceeded {self._deadline:.0f}s deadline"):
            count = self._bump("timed_out")
            logger.warning(
                "[%s] channel stalled until the attempt ends (timed_out=%d, retry depth=%d)",
                self.name, count, self.retry_queue.depth,
            )

    def _release(self, attempt: _Attempt) -> None:
        """Forget a settled attempt so the channel is no longer stalled by it."""
        if attempt is self._current:
            self._current = None

    def _settle(self, attempt: _Attempt, late_entry: QueueEntry | None, elapsed: float) -> None:
        """Record the outcome of a completed attempt (attempt's thread, ``attempt.lock`` held)."""
        if attempt.error is None:
            self._bump("delivered")
            if late_entry is not None:
                if self.retry_queue.discard(late_entry):
                    logger.info(
                        "[%s] delivery completed after %.1fs, past its deadline - retry cancelled",
                        self.name, elapsed,
                    )
            elif attempt.entry is not None:
                self.retry_queue.mark_delivered(attempt.entry)
            return

        self._bump("failed")
        if late_entry is not None:
            # Already queued for retry when the deadline passed.
            logger.warning(
                "[%s] late delivery attempt failed after %.1fs: %s",
                self.name, elapsed, attempt.error,
            )
        elif attempt.entry is not None:
            self.retry_queue.mark_failed(attempt.entry)
        else:
            self.retry_queue.enqueue(attempt.event, attempt.notifier)
            logger.warning(
                "[%s] %s failed - event queued for retry (queue depth: %d): %s",
                self.name, type(attempt.notifier).__name__, self.retry_queue.depth, attempt.error,
            )


class ChannelDispatcher:
    """Owns one :class:`ChannelWorker` per enabled channel."""

    def __init__(
        self,
        retry_queue: NotificationQueue,
        *,
        max_pending: int = DEFAULT_QUEUE_SIZE,
        send_deadline: float = DEFAULT_SEND_DEADLINE,
        retry_interval: float = WORKER_INTERVAL,
        stop_grace: float = STOP_GRACE,
    ) -> None:
        self._retry_queue = retry_queue
        self._max_pending = max_pending
        self._send_deadline = send_deadline
        self._retry_interval = retry_interval
        self._stop_grace = stop_grace
        self._lock = threading.Lock()
        self._stop_lock = threading.Lock()   # held for the whole of stop()
        self._workers: dict[str, ChannelWorker] = {}
        self._retired: list[ChannelWorker] = []
        self._stopped = False

    @staticmethod
    def channel_name(notifier: Any) -> str:
        return str(getattr(notifier, "name", None) or type(notifier).__name__)

    def _create(self, name: str, notifier: Any, retry_types: frozenset[str]) -> ChannelWorker:
        """Must be called with self._lock held."""
        worker = ChannelWorker(
            name,
            notifier,
            self._retry_queue,
            retry_types=retry_types,
            max_pending=self._max_pending,
            send_deadline=self._send_deadline,
            retry_interval=self._retry_interval,
        )
        self._workers[name] = worker
        worker.start()
        return worker

    def configure(self, max_pending: int, send_deadline: float) -> None:
        """Apply ``notifications.delivery`` bounds to every channel, current and future."""
        with self._lock:
            changed = (max_pending, send_deadline) != (self._max_pending, self._send_deadline)
            self._max_pending = max_pending
            self._send_deadline = send_deadline
            workers = list(self._workers.values())
        for worker in workers:
            worker.configure(max_pending, send_deadline)
        if changed:
            logger.info(
                "Notification delivery bounds: queue_size=%d per channel, send deadline %.0fs",
                max_pending, send_deadline,
            )

    def sync(self, notifiers: list) -> None:
        """Match workers to *notifiers*: start new channels, swap senders, retire removed ones.

        Called at start-up and on every config reload. A retired channel's
        pending events are persisted to the retry queue, where they wait (as
        retry entries always have) until a channel of that name is enabled
        again or they expire.
        """
        wanted: dict[str, tuple[Any, frozenset[str]]] = {}
        seen_classes: set[str] = set()
        for notifier in notifiers:
            name = self.channel_name(notifier)
            if name in wanted:
                continue
            owned = {name}
            class_name = type(notifier).__name__
            if class_name not in seen_classes:
                # Legacy retry entries are keyed by class name; the first
                # channel of each class claims them (as process_due did).
                owned.add(class_name)
                seen_classes.add(class_name)
            wanted[name] = (notifier, frozenset(owned))

        retired: list[ChannelWorker] = []
        with self._lock:
            if self._stopped:
                return
            for name, (notifier, types) in wanted.items():
                worker = self._workers.get(name)
                if worker is None:
                    self._create(name, notifier, types)
                else:
                    worker.notifier = notifier
                    worker.retry_types = types
            for name in list(self._workers):
                if name not in wanted:
                    retired.append(self._workers.pop(name))
            self._retired.extend(retired)
            self._retired = [w for w in self._retired if w.is_alive()]
        for worker in retired:
            worker.stop()
            logger.info("[%s] channel removed from config - delivery worker retired", worker.name)

    def submit(self, notifier: Any, event: dict[str, Any]) -> str:
        """Queue *event* for the channel named by *notifier* without blocking the caller.

        Workers are created and re-pointed only by :meth:`sync`, so a caller
        holding a notifier snapshot from before a config reload can neither
        resurrect a removed channel nor overwrite the freshly loaded sender.
        An event for a channel without a worker goes to the retry queue,
        where it waits like any entry for a channel that is not enabled.
        """
        name = self.channel_name(notifier)
        with self._lock:
            worker = None if self._stopped else self._workers.get(name)
        if worker is None:
            self._retry_queue.enqueue(event, notifier)
            logger.warning(
                "[%s] %s - event written to retry queue",
                name, "dispatcher stopped" if self._stopped else "no delivery worker for channel",
            )
            return "stopped" if self._stopped else "no_worker"
        return worker.submit(event)

    def stop(self, grace: float | None = None) -> None:
        """Persist pending work and stop every worker.

        Safe to call more than once: a second caller (``main()`` after the
        subscriber returns, while the shutdown watcher is already draining)
        blocks until the first stop has finished, so the process never exits
        with a drain half done.
        """
        with self._stop_lock:
            with self._lock:
                if self._stopped:
                    return
                self._stopped = True
                workers = list(self._workers.values()) + [w for w in self._retired if w.is_alive()]
            self._stop_workers(workers, self._stop_grace if grace is None else grace)

    def _stop_workers(self, workers: list[ChannelWorker], grace: float) -> None:
        for worker in workers:
            worker.stop()
        deadline = time.monotonic() + grace
        for worker in workers:
            worker.join(deadline - time.monotonic())
        for worker in workers:
            worker.persist_inflight()
        logger.info(
            "Notification dispatcher stopped - retry queue depth %d: %s",
            self._retry_queue.depth,
            ", ".join(
                f"{w.name}={w.snapshot()}" for w in workers
            ) or "no channels",
        )

    def stop_on(self, shutdown_event: threading.Event) -> None:
        """Run :meth:`stop` as soon as *shutdown_event* is set.

        The subscriber may be blocked in ``pubsub.listen()`` when SIGTERM
        arrives, so the drain must not depend on the main thread reaching
        the end of its loop before the container is killed.
        """

        def _watch() -> None:
            shutdown_event.wait()
            self.stop()

        threading.Thread(target=_watch, name="notif-dispatcher-shutdown", daemon=True).start()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Per-channel counters, live-queue depth and stalled flag."""
        with self._lock:
            workers = dict(self._workers)
        return {name: worker.snapshot() for name, worker in workers.items()}

    def worker(self, name: str) -> ChannelWorker | None:
        with self._lock:
            return self._workers.get(name)
