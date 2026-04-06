"""YOLO model wrapper — loads .pt or .engine files and runs inference."""

import logging
import threading
from dataclasses import dataclass

import numpy as np

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
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        # Import here so the module can be imported without ultralytics installed
        # (e.g., during unit tests that mock the model).
        from ultralytics import YOLO

        logger.info("Loading model: %s", self.model_path)
        self._model = YOLO(self.model_path)
        logger.info("Model ready (classes: %s)", sorted(self.target_classes))

    def predict(
        self,
        frame: np.ndarray,
        target_classes: set[str] | None = None,
    ) -> list[Detection]:
        """Run inference and return detections that pass class + confidence filters.

        Thread-safe: acquires a lock so multiple camera threads share one GPU
        without concurrent model calls.

        If *target_classes* is provided it overrides the instance-level filter,
        allowing cameras that share a model to detect different class subsets.
        """
        with self._lock:
            results = self._model.predict(
                frame,
                conf=self.confidence_threshold,
                verbose=False,
                save=False,
                project="/tmp/runs",
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

