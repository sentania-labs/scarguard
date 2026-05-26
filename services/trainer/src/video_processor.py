"""Video processing: frame extraction + YOLO inference + IoU deduplication.

Standalone module imported by the trainer service's job runner. Does NOT
write to the database — returns data for the caller to persist. This
keeps the module testable without a DB dependency.

Requires: opencv-python-headless, ultralytics, torch.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class RawDetection:
    """A single detection from one frame before deduplication."""

    frame_idx: int
    timestamp_in_video: float
    bbox: list[float]  # [x_center, y_center, width, height] normalized
    predicted_class: str
    confidence: float
    detection_pass: str  # 'low' or 'normal'


@dataclass
class ProcessingResult:
    """Result of processing a single upload."""

    upload_id: str
    frame_count: int
    raw_detection_count: int
    deduped_detection_count: int
    detections: list[RawDetection] = field(default_factory=list)
    error: str | None = None


def compute_iou(box_a: list[float], box_b: list[float]) -> float:
    """IoU for YOLO-format boxes [x_center, y_center, width, height] (normalized)."""
    ax1 = box_a[0] - box_a[2] / 2
    ay1 = box_a[1] - box_a[3] / 2
    ax2 = box_a[0] + box_a[2] / 2
    ay2 = box_a[1] + box_a[3] / 2

    bx1 = box_b[0] - box_b[2] / 2
    by1 = box_b[1] - box_b[3] / 2
    bx2 = box_b[0] + box_b[2] / 2
    by2 = box_b[1] + box_b[3] / 2

    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)

    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = box_a[2] * box_a[3]
    area_b = box_b[2] * box_b[3]
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def dedupe_detections(
    detections: list[RawDetection],
    *,
    iou_threshold: float = 0.85,
    frame_window: int = 5,
) -> list[RawDetection]:
    """Remove near-duplicate detections within a frame window.

    Per class, sorted by frame_idx ascending: for each detection, look back
    at prior detections within *frame_window* frames. If any same-class
    detection has IoU > *iou_threshold*, keep the one with higher confidence.
    """
    by_class: dict[str, list[RawDetection]] = {}
    for d in detections:
        by_class.setdefault(d.predicted_class, []).append(d)

    survivors: list[RawDetection] = []

    for cls_dets in by_class.values():
        cls_dets.sort(key=lambda d: d.frame_idx)
        dropped: set[int] = set()

        for i, det in enumerate(cls_dets):
            if i in dropped:
                continue
            for j in range(i - 1, -1, -1):
                if j in dropped:
                    continue
                prior = cls_dets[j]
                if det.frame_idx - prior.frame_idx > frame_window:
                    break
                if compute_iou(det.bbox, prior.bbox) > iou_threshold:
                    if det.confidence >= prior.confidence:
                        dropped.add(j)
                    else:
                        dropped.add(i)
                    break

        survivors.extend(d for idx, d in enumerate(cls_dets) if idx not in dropped)

    survivors.sort(key=lambda d: (d.frame_idx, d.predicted_class))
    return survivors


def extract_and_infer(
    video_path: Path,
    frames_dir: Path,
    model_path: str,
    *,
    confidence_threshold: float,
    low_confidence: float = 0.05,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[int, list[RawDetection]]:
    """Extract every frame from *video_path*, run YOLO inference, return detections.

    Each frame is saved as ``{frame_idx:06d}.jpg`` in *frames_dir*.
    Single inference pass at *low_confidence*; detections tagged ``'normal'``
    if confidence >= *confidence_threshold*, else ``'low'``.
    """
    import cv2
    from ultralytics import YOLO

    frames_dir.mkdir(parents=True, exist_ok=True)
    model = YOLO(model_path)

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0

    raw_detections: list[RawDetection] = []
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_path = frames_dir / f"{frame_idx:06d}.jpg"
        cv2.imwrite(str(frame_path), frame)

        results = model.predict(
            frame,
            conf=low_confidence,
            verbose=False,
            save=False,
            project="/tmp/runs",
            name="predict",
            exist_ok=True,
        )

        timestamp = frame_idx / fps

        for result in results:
            for box in result.boxes:
                conf = float(box.conf[0])
                class_name: str = result.names[int(box.cls[0])]
                xyxy = box.xyxyn[0].tolist()
                x_center = (xyxy[0] + xyxy[2]) / 2
                y_center = (xyxy[1] + xyxy[3]) / 2
                width = xyxy[2] - xyxy[0]
                height = xyxy[3] - xyxy[1]
                raw_detections.append(RawDetection(
                    frame_idx=frame_idx,
                    timestamp_in_video=round(timestamp, 3),
                    bbox=[round(x_center, 6), round(y_center, 6), round(width, 6), round(height, 6)],
                    predicted_class=class_name,
                    confidence=round(conf, 4),
                    detection_pass="normal" if conf >= confidence_threshold else "low",
                ))

        frame_idx += 1
        if progress_callback and frame_idx % 50 == 0:
            progress_callback(frame_idx, total_frames)

    cap.release()

    del model
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass

    if progress_callback:
        progress_callback(frame_idx, frame_idx)

    return frame_idx, raw_detections


def process_upload(
    upload_id: str,
    video_path: Path,
    frames_dir: Path,
    model_path: str,
    *,
    confidence_threshold: float,
    low_confidence: float = 0.05,
    dedupe_iou: float = 0.85,
    dedupe_window: int = 5,
    target_class_hint: str | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> ProcessingResult:
    """Full pipeline: extract frames → infer → dedupe → return result."""
    try:
        frame_count, raw = extract_and_infer(
            video_path,
            frames_dir,
            model_path,
            confidence_threshold=confidence_threshold,
            low_confidence=low_confidence,
            progress_callback=progress_callback,
        )
        deduped = dedupe_detections(raw, iou_threshold=dedupe_iou, frame_window=dedupe_window)
        if target_class_hint:
            for d in deduped:
                d.detection_pass = d.detection_pass  # preserve; hint is metadata only
        return ProcessingResult(
            upload_id=upload_id,
            frame_count=frame_count,
            raw_detection_count=len(raw),
            deduped_detection_count=len(deduped),
            detections=deduped,
        )
    except Exception as e:
        logger.exception("Video processing failed for upload %s", upload_id)
        return ProcessingResult(
            upload_id=upload_id,
            frame_count=0,
            raw_detection_count=0,
            deduped_detection_count=0,
            error=str(e),
        )


def result_to_db_rows(
    result: ProcessingResult,
    target_class_hint: str | None = None,
) -> list[dict[str, Any]]:
    """Convert ProcessingResult detections to dicts suitable for db.insert_training_events."""
    return [
        {
            "frame_idx": d.frame_idx,
            "timestamp_in_video": d.timestamp_in_video,
            "bbox": json.dumps(d.bbox),
            "predicted_class": d.predicted_class,
            "confidence": d.confidence,
            "target_class_hint": target_class_hint,
            "detection_pass": d.detection_pass,
        }
        for d in result.detections
    ]
