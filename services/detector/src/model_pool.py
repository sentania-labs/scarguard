"""Manages multiple YOLODetector instances keyed by model path.

Reference-counted so that models shared by several cameras are loaded once and
unloaded when no camera references them any more.  Thread-safe — the pool lock
serialises load/unload operations while each YOLODetector uses its own inference
lock for GPU access.
"""

from __future__ import annotations

import logging
import os
import threading

from detector import YOLODetector

logger = logging.getLogger(__name__)


def _clear_gpu_cache() -> None:
    """Release cached GPU memory after model deletion."""
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


class ModelPool:
    """Lazy-loading, ref-counted pool of YOLO detectors."""

    def __init__(
        self,
        default_model_path: str,
        default_confidence: float,
        default_classes: list[str],
    ) -> None:
        self._default_model_path = default_model_path
        self._default_confidence = default_confidence
        self._default_classes = list(default_classes)
        self._models: dict[str, YOLODetector] = {}
        self._refcounts: dict[str, int] = {}
        self._lock = threading.Lock()

    # -- Public API ----------------------------------------------------------

    def get_detector(self, model_path: str | None = None) -> YOLODetector:
        """Return a shared :class:`YOLODetector` for *model_path*, loading it if necessary.

        Increments the internal reference count.  Call :meth:`release` when the
        camera using this detector is stopped.
        """
        path = model_path or self._default_model_path
        with self._lock:
            if path not in self._models:
                logger.info("ModelPool: loading %s", path)
                try:
                    self._models[path] = YOLODetector(
                        model_path=path,
                        confidence_threshold=self._default_confidence,
                        target_classes=self._default_classes,
                    )
                except Exception:
                    logger.exception("ModelPool: failed to load model %s", path)
                    raise
                self._refcounts[path] = 0
            self._refcounts[path] += 1
            return self._models[path]

    def release(self, model_path: str | None = None) -> None:
        """Decrement the reference count for *model_path* and unload if zero."""
        path = model_path or self._default_model_path
        with self._lock:
            if path not in self._refcounts:
                logger.warning("ModelPool: release called for untracked model %s (double-release?)", path)
                return
            self._refcounts[path] -= 1
            if self._refcounts[path] <= 0:
                del self._models[path]
                del self._refcounts[path]
                _clear_gpu_cache()
                logger.info("ModelPool: unloaded %s (no cameras using it)", path)

    def update_defaults(
        self,
        model_path: str,
        confidence: float,
        target_classes: list[str],
    ) -> None:
        """Update the global defaults (called on config hot-reload).

        Also updates confidence on any already-loaded detectors so the change
        takes effect without a camera restart.
        """
        with self._lock:
            self._default_model_path = model_path
            self._default_confidence = confidence
            self._default_classes = list(target_classes)
            for det in self._models.values():
                det.confidence_threshold = confidence
                det.target_classes = set(target_classes)

    @staticmethod
    def validate_model_exists(model_path: str) -> bool:
        """Return True if *model_path* is a readable file on disk."""
        return os.path.isfile(model_path)
