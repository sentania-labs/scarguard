"""Model evaluation runner — compares two YOLO models against labeled snapshots.

Listens on Redis channel ``scarguard:eval:request`` for evaluation requests from
the web service.  Runs inference with each model sequentially (to conserve GPU
memory on the Jetson), computes per-class precision/recall/mAP@0.5, and publishes
results back via Redis keys.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

import redis

logger = logging.getLogger(__name__)

REQUEST_CHANNEL = "scarguard:eval:request"
PROGRESS_KEY = "scarguard:eval:progress"
RESULT_KEY = "scarguard:eval:result"
RESULT_TTL = 3600  # 1 hour


class EvaluationRunner:
    """Background thread that processes model evaluation requests via Redis."""

    def __init__(
        self,
        redis_cfg: dict,
        db_path: str,
        snapshot_dir: str,
    ) -> None:
        self._redis_cfg = redis_cfg
        self._db_path = db_path
        self._snapshot_dir = Path(snapshot_dir)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._busy = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="evaluator", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _get_redis(self) -> redis.Redis:
        return redis.Redis(
            host=self._redis_cfg.get("host", "redis"),
            port=int(self._redis_cfg.get("port", 6379)),
            decode_responses=True,
        )

    def _run(self) -> None:
        logger.info("EvaluationRunner started — listening on %s", REQUEST_CHANNEL)
        while not self._stop.is_set():
            try:
                client = self._get_redis()
                pubsub = client.pubsub()
                pubsub.subscribe(REQUEST_CHANNEL)
                for message in pubsub.listen():
                    if self._stop.is_set():
                        break
                    if message["type"] != "message":
                        continue
                    try:
                        request = json.loads(message["data"])
                        if not self._busy.acquire(blocking=False):
                            self._publish_error(client, "Evaluation already in progress")
                            continue
                        try:
                            self._handle_request(client, request)
                        finally:
                            self._busy.release()
                    except Exception:
                        logger.exception("Error processing evaluation request")
                        self._publish_error(client, "Internal error during evaluation")
                pubsub.unsubscribe(REQUEST_CHANNEL)
                client.close()
            except redis.ConnectionError:
                if not self._stop.is_set():
                    logger.warning("Redis connection lost in evaluator — retrying in 5s")
                    self._stop.wait(5)
            except Exception:
                logger.exception("Unexpected error in evaluator loop")
                self._stop.wait(5)

    def _handle_request(self, client: redis.Redis, request: dict) -> None:
        model_a_path = request.get("model_a", "")
        model_b_path = request.get("model_b", "")
        date_from = request.get("date_from")
        date_to = request.get("date_to")

        logger.info(
            "Evaluation request: model_a=%s model_b=%s date_from=%s date_to=%s",
            model_a_path, model_b_path, date_from, date_to,
        )

        # Load ground truth from DB
        ground_truth = self._load_ground_truth(date_from, date_to)
        if not ground_truth:
            self._publish_error(client, "No labeled snapshots found for the selected date range")
            return

        self._set_progress(client, "loading", 0, len(ground_truth) * 2)

        # Run model A
        logger.info("Running model A: %s on %d snapshots", model_a_path, len(ground_truth))
        preds_a = self._run_model(client, model_a_path, ground_truth, 0, "Model A")
        if preds_a is None:
            return

        # Run model B
        logger.info("Running model B: %s on %d snapshots", model_b_path, len(ground_truth))
        preds_b = self._run_model(
            client, model_b_path, ground_truth, len(ground_truth), "Model B",
        )
        if preds_b is None:
            return

        # Compute metrics
        self._set_progress(client, "computing", len(ground_truth) * 2, len(ground_truth) * 2)
        metrics_a = self._compute_metrics(ground_truth, preds_a)
        metrics_b = self._compute_metrics(ground_truth, preds_b)

        # Collect sample detections (up to 10)
        samples = self._collect_samples(ground_truth, preds_a, preds_b)

        result = {
            "status": "complete",
            "model_a": {"path": model_a_path, "metrics": metrics_a},
            "model_b": {"path": model_b_path, "metrics": metrics_b},
            "total_snapshots": len(ground_truth),
            "samples": samples,
        }
        client.set(RESULT_KEY, json.dumps(result), ex=RESULT_TTL)
        self._set_progress(client, "complete", len(ground_truth) * 2, len(ground_truth) * 2)
        logger.info("Evaluation complete — results published")

    def _load_ground_truth(
        self,
        date_from: str | None,
        date_to: str | None,
    ) -> list[dict]:
        """Load labeled events with bbox data from SQLite."""
        where = (
            "camera_name != '_system'"
            " AND feedback IN ('correct', 'wrong_class')"
            " AND bbox IS NOT NULL"
            " AND snapshot_path IS NOT NULL"
        )
        params: list[object] = []
        if date_from:
            where += " AND timestamp >= ?"
            params.append(date_from)
        if date_to:
            where += " AND timestamp < ?"
            params.append(date_to)

        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"""
                SELECT id, class_name, confidence, camera_name,
                       snapshot_path, bbox, frame_size,
                       feedback, corrected_class
                FROM detection_events
                WHERE {where}
                ORDER BY id
                """,
                params,
            ).fetchall()
        finally:
            conn.close()

        result: list[dict] = []
        for r in rows:
            row = dict(r)
            row["bbox"] = json.loads(row["bbox"]) if isinstance(row["bbox"], str) else row["bbox"]
            row["frame_size"] = json.loads(row["frame_size"]) if isinstance(row["frame_size"], str) else row["frame_size"]
            # Use corrected class for wrong_class feedback
            if row.get("feedback") == "wrong_class" and row.get("corrected_class"):
                row["effective_class"] = row["corrected_class"]
            else:
                row["effective_class"] = row["class_name"]
            # Resolve snapshot path
            snap_path = Path(row["snapshot_path"])
            if not snap_path.exists():
                snap_path = self._snapshot_dir / snap_path.name
            if snap_path.exists():
                row["resolved_path"] = str(snap_path)
                result.append(row)
            else:
                logger.debug("Skipping event %d — snapshot not found: %s", row["id"], row["snapshot_path"])

        return result

    def _run_model(
        self,
        client: redis.Redis,
        model_path: str,
        ground_truth: list[dict],
        progress_offset: int,
        model_label: str,
    ) -> list[list[dict]] | None:
        """Run a YOLO model against all snapshots. Returns predictions per snapshot."""
        try:
            import cv2
            from ultralytics import YOLO
        except ImportError as e:
            self._publish_error(client, f"Missing dependency: {e}")
            return None

        try:
            model = YOLO(model_path)
        except Exception as e:
            self._publish_error(client, f"Failed to load {model_label} ({model_path}): {e}")
            return None

        total = len(ground_truth) * 2
        all_predictions: list[list[dict]] = []

        for i, gt in enumerate(ground_truth):
            if self._stop.is_set():
                return None

            self._set_progress(
                client, f"running {model_label}", progress_offset + i, total,
            )

            frame = cv2.imread(gt["resolved_path"])
            if frame is None:
                all_predictions.append([])
                continue

            try:
                results = model.predict(frame, conf=0.1, verbose=False, save=False)
            except Exception:
                logger.exception("Inference failed for snapshot %s", gt["resolved_path"])
                all_predictions.append([])
                continue

            dets: list[dict] = []
            for result in results:
                for box in result.boxes:
                    class_name: str = result.names[int(box.cls)]
                    dets.append({
                        "class_name": class_name,
                        "confidence": float(box.conf),
                        "bbox": [int(v) for v in box.xyxy[0]],
                    })
            all_predictions.append(dets)

        # Explicitly delete model and free GPU memory
        del model
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
        return all_predictions

    def _compute_metrics(
        self,
        ground_truth: list[dict],
        predictions: list[list[dict]],
    ) -> dict:
        """Compute per-class precision, recall, and mAP@0.5."""
        # Collect all class names from ground truth
        all_classes: set[str] = {gt["effective_class"] for gt in ground_truth}

        per_class: dict[str, dict] = {}
        for cls in sorted(all_classes):
            tp = 0
            fp = 0
            fn = 0
            for gt, preds in zip(ground_truth, predictions):
                gt_cls = gt["effective_class"]
                gt_bbox = gt["bbox"]

                # Filter predictions to this class
                cls_preds = [p for p in preds if p["class_name"] == cls]

                if gt_cls == cls:
                    # There's a ground truth for this class in this image
                    matched = False
                    for pred in cls_preds:
                        if _iou(gt_bbox, pred["bbox"]) >= 0.5:
                            matched = True
                            break
                    if matched:
                        tp += 1
                        # Count remaining predictions as FP
                        fp += max(0, len(cls_preds) - 1)
                    else:
                        fn += 1
                        fp += len(cls_preds)
                else:
                    # No ground truth for this class here — all predictions are FP
                    fp += len(cls_preds)

            precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
            per_class[cls] = {
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "f1": round(f1, 4),
                "tp": tp,
                "fp": fp,
                "fn": fn,
            }

        # Overall averages (precision at IoU>=0.5 threshold, not full mAP curve)
        precisions = [m["precision"] for m in per_class.values()]
        recalls = [m["recall"] for m in per_class.values()]
        mean_prec = sum(precisions) / len(precisions) if precisions else 0.0
        mean_rec = sum(recalls) / len(recalls) if recalls else 0.0
        f1_scores = [m["f1"] for m in per_class.values()]
        mean_f1 = sum(f1_scores) / len(f1_scores) if f1_scores else 0.0

        return {
            "per_class": per_class,
            "mean_precision": round(mean_prec, 4),
            "mean_recall": round(mean_rec, 4),
            "mean_f1": round(mean_f1, 4),
        }

    def _collect_samples(
        self,
        ground_truth: list[dict],
        preds_a: list[list[dict]],
        preds_b: list[list[dict]],
        max_samples: int = 10,
    ) -> list[dict]:
        """Collect sample detection data with snapshot filenames (not base64).

        Uses snapshot filenames so the web service can serve images from disk
        via the existing ``/snapshots/`` static mount, avoiding large payloads.
        """
        samples: list[dict] = []
        step = max(1, len(ground_truth) // max_samples)
        for i in range(0, len(ground_truth), step):
            if len(samples) >= max_samples:
                break
            gt = ground_truth[i]
            snap_filename = Path(gt["resolved_path"]).name
            samples.append({
                "snapshot_filename": snap_filename,
                "ground_truth": {
                    "class_name": gt["effective_class"],
                    "bbox": gt["bbox"],
                },
                "predictions_a": preds_a[i] if i < len(preds_a) else [],
                "predictions_b": preds_b[i] if i < len(preds_b) else [],
            })
        return samples

    def _set_progress(
        self, client: redis.Redis, status: str, current: int, total: int,
    ) -> None:
        progress = {
            "status": status,
            "current": current,
            "total": total,
            "pct": round((current / total) * 100, 1) if total > 0 else 0,
        }
        client.set(PROGRESS_KEY, json.dumps(progress), ex=300)

    def _publish_error(self, client: redis.Redis, message: str) -> None:
        result = {"status": "error", "error": message}
        client.set(RESULT_KEY, json.dumps(result), ex=RESULT_TTL)
        self._set_progress(client, "error", 0, 0)
        logger.error("Evaluation error: %s", message)


def _iou(box_a: list[int], box_b: list[int]) -> float:
    """Compute Intersection over Union for two [x1, y1, x2, y2] boxes."""
    x1 = max(box_a[0], box_b[0])
    y1 = max(box_a[1], box_b[1])
    x2 = min(box_a[2], box_b[2])
    y2 = min(box_a[3], box_b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
    area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0
