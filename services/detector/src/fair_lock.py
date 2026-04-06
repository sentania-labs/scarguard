"""FIFO fair lock — prevents thread starvation under contention.

Python's ``threading.Lock()`` does not guarantee FIFO ordering, so a
high-frequency thread can monopolize the lock while slower threads starve.
``FairLock`` uses an internal queue of ``threading.Event`` objects to ensure
waiting threads are woken in the order they arrived.

Used by ``YOLODetector`` to ensure fair GPU access across camera threads
that share the same model.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class FairLock:
    """Lock that guarantees FIFO acquisition order."""

    __slots__ = ("_lock", "_queue", "_held")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: deque[threading.Event] = deque()
        self._held = False

    def acquire(self, timeout: float = 30.0) -> float:
        """Acquire the lock, blocking until it is available.

        Returns the wall-clock seconds spent waiting (0.0 if uncontended).
        Raises ``TimeoutError`` if the lock is not acquired within *timeout*
        seconds (prevents permanent hangs if a holder crashes).
        """
        t0 = time.monotonic()
        event = threading.Event()
        with self._lock:
            if not self._held:
                self._held = True
                event.set()
            else:
                self._queue.append(event)
        if not event.wait(timeout=timeout):
            # Remove ourselves from the queue to avoid a dangling event.
            # Race: release() may have popped & signaled us between wait()
            # returning False and us acquiring _lock.  If so the event is
            # now set and ownership has been handed to us — accept it.
            with self._lock:
                try:
                    self._queue.remove(event)
                except ValueError:
                    if event.is_set():
                        return time.monotonic() - t0
            raise TimeoutError(f"FairLock.acquire timed out after {timeout:.0f}s")
        return time.monotonic() - t0

    def release(self) -> None:
        """Release the lock and wake the next waiting thread, if any."""
        with self._lock:
            if self._queue:
                next_event = self._queue.popleft()
                next_event.set()
            else:
                self._held = False
