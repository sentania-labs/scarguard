"""Thread-safe mutable reference — replaces GIL-dependent list[T] wrappers."""

from __future__ import annotations

import threading
from typing import Generic, TypeVar

T = TypeVar("T")


class AtomicRef(Generic[T]):
    """A thread-safe container for a single value.

    Replaces the ``list[T]`` single-element wrapper pattern that relied on
    CPython's GIL for atomicity.  Uses an internal lock so correctness is
    guaranteed regardless of the Python implementation.
    """

    __slots__ = ("_value", "_lock")

    def __init__(self, value: T) -> None:
        self._value = value
        self._lock = threading.Lock()

    def get(self) -> T:
        with self._lock:
            return self._value

    def set(self, value: T) -> None:
        with self._lock:
            self._value = value
