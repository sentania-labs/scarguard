"""YOLO model wrapper — loads .pt or .engine files and runs inference."""

import logging
from dataclasses import dataclass

import numpy as np
from fair_lock import FairLock

logger = logging.getLogger(__name__)


@dataclass
class Detection:
    class_name: str
    confidence: float
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2


class YOLODetector:
    def __init__(
        self,
        model_path: str,
        confidence_threshold: float,
        target_classes: list[str],
    ) -> None:
        self.model_path = model_path
        self.confidence_threshold = confidence_threshold
        self.target_classes = set(target_classes)
        self._model = None
        self._lock = FairLock()
        self._load()

    def _load(self) -> None:
        # Import here so the module can be imported without ultralytics installed
        # (e.g., during unit tests that mock the model).
        from ultralytics import YOLO

        logger.info("Loading model: %s", self.model_path)
        self._model = YOLO(self.model_path)
        logger.info("Model ready (classes: %s)", sorted(self.target_classes))
        # v0.12.7: predict() uses a pinned save_dir to avoid ultralytics'
        # increment_path O(N) scan.  Log the pin so the fix is visible in
        # production logs.  See INFERENCE_INVESTIGATION.md.
        logger.info("Inference save_dir pinned to /tmp/runs/predict (exist_ok=True)")

    def predict(
        self,
        frame: np.ndarray,
        target_classes: set[str] | None = None,
        confidence: float | None = None,
    ) -> list[Detection]:
        """Run inference and return detections that pass class + confidence filters.

        Thread-safe: acquires a lock so multiple camera threads share one GPU
        without concurrent model calls.

        If *target_classes* is provided it overrides the instance-level filter,
        allowing cameras that share a model to detect different class subsets.
        If *confidence* is provided it overrides the instance-level
        confidence_threshold for this call only — enables per-camera
        confidence tuning on a shared detector.
        """
        conf = confidence if confidence is not None else self.confidence_threshold
        wait_seconds = self._lock.acquire()
        try:
            # NOTE: name + exist_ok are critical.  Ultralytics 8.3.x's Predictor
            # unconditionally calls increment_path(Path(project) / name) in its
            # constructor, which creates a fresh predict{N} subdirectory on every
            # call even when save=False, and on subsequent calls stats every
            # existing predict{N} via os.path.exists() to find the next free
            # integer.  Under sustained load the directory count grows without
            # bound and each predict() call becomes O(N) in filesystem syscalls.
            # (We hit this in production — see INFERENCE_INVESTIGATION.md.)
            # exist_ok=True makes increment_path reuse /tmp/runs/predict and
            # short-circuits the scan loop on the very first iteration.
            results = self._model.predict(
                frame,
                conf=conf,
                verbose=False,
                save=False,
                project="/tmp/runs",
                name="predict",
                exist_ok=True,
            )
        finally:
            self._lock.release()

        if wait_seconds > 0.5:
            logger.info(
                "Inference lock wait: %.0f ms (model=%s)",
                wait_seconds * 1000,
                self.model_path,
            )

        classes = target_classes if target_classes is not None else self.target_classes
        detections: list[Detection] = []
        for result in results:
            for box in result.boxes:
                class_name: str = result.names[int(box.cls)]
                if classes and class_name not in classes:
                    continue
                confidence = float(box.conf)
                x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])
                detections.append(Detection(class_name, confidence, (x1, y1, x2, y2)))

        return detections

