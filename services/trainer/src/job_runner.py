"""Job runner — dispatches training job types with pause/resume lifecycle.

Each job type runs the appropriate pipeline step:
- process_video: pause → YOLO inference on uploaded video → resume
- prepare_dataset: CPU work (no pause needed)
- train: pause → fine-tune YOLO model → resume
- prepare_and_train: prepare_dataset then train
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pause_protocol import PauseClient
from redis_client import make_sync_client

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/scarguard.db")
MODELS_DIR = os.environ.get("MODELS_DIR", "/models")
TRAINING_UPLOADS_DIR = os.environ.get("TRAINING_UPLOADS_DIR", "/data/training_uploads")
WORKSPACE_DIR = Path(os.environ.get("TRAINING_WORKSPACE_DIR", "/data/training_workspace"))
SCRIPTS_DIR = Path("/app/scripts")

_PROGRESS_TTL = 300
_LOG_TTL = 3600
_LOG_CAP = 500

# Mirrors the web form's class-name validation (routes/training_jobs.py).
_CLASS_TOKEN_RE = re.compile(r"^[a-z0-9_-]+$")


def _progress_key(job_id: str) -> str:
    return f"scarguard:training:job:{job_id}:progress"


def _log_key(job_id: str) -> str:
    return f"scarguard:training:job:{job_id}:log"


def _cancel_key(job_id: str) -> str:
    return f"scarguard:training:job:{job_id}:cancel"


def _connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


class JobContext:
    """Shared state for a running job."""

    def __init__(self, job: dict, cfg: dict, stop_event: threading.Event) -> None:
        self.job = job
        self.job_id: str = job["id"]
        self.job_type: str = job["type"]
        self.params: dict = json.loads(job["params"]) if isinstance(job["params"], str) else job["params"]
        self.cfg = cfg
        self.stop_event = stop_event
        self.redis_cfg = cfg.get("redis", {})
        self._redis = make_sync_client(self.redis_cfg)
        self._pause_client = PauseClient(self.redis_cfg)

    def close(self) -> None:
        try:
            self._redis.close()
        except Exception:
            pass

    def publish_progress(self, phase: str, pct: float, detail: str = "", **extra: Any) -> None:
        data = {"phase": phase, "pct": round(pct, 1), "detail": detail, **extra}
        self._redis.set(_progress_key(self.job_id), json.dumps(data), ex=_PROGRESS_TTL)

    def append_log(self, line: str) -> None:
        key = _log_key(self.job_id)
        self._redis.rpush(key, line)
        self._redis.ltrim(key, -_LOG_CAP, -1)
        self._redis.expire(key, _LOG_TTL)

    def is_cancelled(self) -> bool:
        return bool(self._redis.exists(_cancel_key(self.job_id))) or self.stop_event.is_set()

    def pause_detector(self, timeout: int = 7200) -> bool:
        ok = self._pause_client.pause(timeout=timeout)
        if ok:
            self._pause_client.start_heartbeat()
        return ok

    def resume_detector(self) -> None:
        self._pause_client.stop_heartbeat()
        self._pause_client.resume()

    def training_config(self) -> dict:
        return self.cfg.get("training", {})

    def detection_config(self) -> dict:
        return self.cfg.get("detection", {})


def run_job(job: dict, cfg: dict, stop_event: threading.Event) -> dict:
    """Dispatch a job by type. Returns result dict."""
    ctx = JobContext(job, cfg, stop_event)
    try:
        if ctx.job_type == "process_video":
            return _run_process_video(ctx)
        elif ctx.job_type == "prepare_dataset":
            return _run_prepare_dataset(ctx)
        elif ctx.job_type == "train":
            return _run_train(ctx)
        elif ctx.job_type == "prepare_and_train":
            return _run_prepare_and_train(ctx)
        else:
            return {"error": f"Unknown job type: {ctx.job_type}"}
    finally:
        ctx.close()


# ── process_video ────────────────────────────────────────────────────────────


def _resolve_upload_model_path(name: str | None, default_path: str) -> str:
    """Resolve a per-upload detector model name to an absolute path.

    Whitelists against the models directory listing to keep the trainer from
    loading arbitrary attacker-controlled paths even though the field is
    admin-set.
    """
    if not name:
        return default_path
    candidate = Path(MODELS_DIR) / Path(name).name
    if candidate.exists() and candidate.is_file():
        return str(candidate)
    logger.warning("Upload model %r not found in %s — falling back to default", name, MODELS_DIR)
    return default_path


def _run_process_video(ctx: JobContext) -> dict:
    from video_processor import process_upload, result_to_db_rows

    upload_ids: list[str] = ctx.params.get("upload_ids", [])
    clear_existing = bool(ctx.params.get("clear_existing_events", False))
    if not upload_ids:
        conn = _connect_db()
        try:
            rows = conn.execute(
                "SELECT id FROM training_uploads WHERE status = 'uploaded' ORDER BY created_at"
            ).fetchall()
            upload_ids = [r["id"] for r in rows]
        finally:
            conn.close()
        if not upload_ids:
            return {"error": "No unprocessed uploads found"}

    det_cfg = ctx.detection_config()
    train_cfg = ctx.training_config()
    video_cfg = train_cfg.get("video", {})
    default_model_path = det_cfg.get("model_path", f"{MODELS_DIR}/yolov8n.pt")
    default_conf_threshold = det_cfg.get("confidence_threshold", 0.25)
    low_conf = ctx.params.get("low_confidence", video_cfg.get("low_confidence", 0.05))
    dedupe_iou = ctx.params.get("dedupe_iou", video_cfg.get("dedupe_iou", 0.85))
    dedupe_window = ctx.params.get("dedupe_window", video_cfg.get("dedupe_window", 5))

    ctx.publish_progress("pausing", 0, "Pausing detector for GPU access")
    if not ctx.pause_detector():
        return {"error": "Failed to pause detector — timeout waiting for ack"}

    try:
        results = []
        for i, uid in enumerate(upload_ids):
            if ctx.is_cancelled():
                return {"error": "Job cancelled", "processed": i}

            conn = _connect_db()
            try:
                upload = conn.execute("SELECT * FROM training_uploads WHERE id = ?", (uid,)).fetchone()
            finally:
                conn.close()
            if not upload:
                logger.warning("Upload %s not found — skipping", uid)
                continue

            upload_dir = Path(TRAINING_UPLOADS_DIR) / uid
            video_files = list(upload_dir.glob("original.*"))
            if not video_files:
                logger.warning("No video file for upload %s", uid)
                continue

            # Per-upload settings; fall back to job/global defaults when NULL.
            upload_keys = upload.keys() if hasattr(upload, "keys") else []
            per_model = upload["detector_model"] if "detector_model" in upload_keys else None
            per_conf = upload["confidence_threshold"] if "confidence_threshold" in upload_keys else None
            model_path = _resolve_upload_model_path(per_model, default_model_path)
            conf_threshold = per_conf if per_conf is not None else default_conf_threshold

            if clear_existing:
                conn = _connect_db()
                try:
                    # Detector rows only — manual frame-browser annotations
                    # (detection_pass='manual') are user work and must survive
                    # a reprocess. Reprocessing only regenerates detector
                    # output; manual rows are independent of model state.
                    deleted = conn.execute(
                        "DELETE FROM training_events "
                        "WHERE upload_id = ? AND detection_pass != 'manual'",
                        (uid,),
                    ).rowcount
                    conn.commit()
                finally:
                    conn.close()
                if deleted:
                    logger.info("Cleared %d existing detections for upload %s", deleted, uid)

            frames_dir = upload_dir / "frames"
            ctx.publish_progress(
                "processing",
                (i / len(upload_ids)) * 100,
                f"Processing {upload['filename']} ({i + 1}/{len(upload_ids)})",
            )

            def _progress(current: int, total: int) -> None:
                if total > 0:
                    pct = (i / len(upload_ids) + (current / total) / len(upload_ids)) * 100
                    ctx.publish_progress("processing", pct, f"Frame {current}/{total}")

            result = process_upload(
                upload_id=uid,
                video_path=video_files[0],
                frames_dir=frames_dir,
                model_path=model_path,
                confidence_threshold=conf_threshold,
                low_confidence=low_conf,
                dedupe_iou=dedupe_iou,
                dedupe_window=dedupe_window,
                target_class_hint=upload["target_class_hint"],
                progress_callback=_progress,
            )

            if result.error:
                logger.error("Processing failed for %s: %s", uid, result.error)
                _update_upload_status(uid, "failed", error=result.error)
                continue

            rows = result_to_db_rows(result, upload["target_class_hint"])
            if rows:
                _insert_training_events(uid, rows)
            _update_upload_status(
                uid, "processed",
                frame_count=result.frame_count,
                detection_count=result.deduped_detection_count,
            )
            results.append({
                "upload_id": uid,
                "frames": result.frame_count,
                "detections": result.deduped_detection_count,
            })

        ctx.publish_progress("complete", 100, f"Processed {len(results)} upload(s)")
        return {"uploads": results}
    finally:
        ctx.resume_detector()


def _update_upload_status(
    upload_id: str, status: str, *,
    frame_count: int | None = None,
    detection_count: int | None = None,
    error: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    sets = ["status = ?"]
    params: list[object] = [status]
    if frame_count is not None:
        sets.append("frame_count = ?")
        params.append(frame_count)
    if detection_count is not None:
        sets.append("detection_count = ?")
        params.append(detection_count)
    if error is not None:
        sets.append("error = ?")
        params.append(error)
    if status in ("processed", "failed"):
        sets.append("processed_at = ?")
        params.append(now)
    params.append(upload_id)
    conn = _connect_db()
    try:
        conn.execute(f"UPDATE training_uploads SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
    finally:
        conn.close()


def _insert_training_events(upload_id: str, events: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect_db()
    try:
        conn.executemany(
            """
            INSERT INTO training_events
                (upload_id, frame_idx, timestamp_in_video, bbox, predicted_class,
                 confidence, target_class_hint, detection_pass, review_state, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            [
                (
                    upload_id,
                    e["frame_idx"],
                    e.get("timestamp_in_video"),
                    e["bbox"],
                    e["predicted_class"],
                    e["confidence"],
                    e.get("target_class_hint"),
                    e["detection_pass"],
                    now,
                )
                for e in events
            ],
        )
        conn.commit()
    finally:
        conn.close()


# ── prepare_dataset ──────────────────────────────────────────────────────────


def _run_prepare_dataset(ctx: JobContext) -> dict:
    train_cfg = ctx.training_config()
    sources = train_cfg.get("sources", {})
    defaults = train_cfg.get("defaults", {})
    output_dir = WORKSPACE_DIR / "merged_dataset"

    cmd = [
        "python3", str(SCRIPTS_DIR / "prepare_dataset.py"),
        "--local-db", DB_PATH,
        "--local-snapshots", "/data/snapshots",
        "--training-uploads-db", DB_PATH,
        "--training-uploads-frames", TRAINING_UPLOADS_DIR,
        "--output", str(output_dir),
        "--val-split", str(ctx.params.get("val_split", defaults.get("val_split", 0.15))),
        "--seed", str(ctx.params.get("seed", 42)),
        "--background-sample-interval", str(
            ctx.params.get("background_sample_interval",
                           train_cfg.get("video", {}).get("background_sample_interval", 10))
        ),
    ]

    # Class list: job param wins, then training.defaults.classes, then the
    # script's built-in default. YAML gives a list, the jobs form a
    # comma-string — accept both, and fail fast on malformed values
    # rather than preparing a near-empty dataset from unmatched labels.
    classes = ctx.params.get("classes") or defaults.get("classes")
    if classes:
        raw = (
            [str(c) for c in classes]
            if isinstance(classes, (list, tuple))
            else re.split(r"[,\s]+", str(classes))
        )
        tokens: list[str] = []
        for part in raw:
            token = part.strip().lower()
            if not token:
                continue
            if not _CLASS_TOKEN_RE.match(token):
                return {"error": f"Invalid class name in training classes: {token!r}"}
            if token not in tokens:
                tokens.append(token)
        if tokens:
            cmd += ["--classes", ",".join(tokens)]

    rf_cfg = sources.get("roboflow", {})
    rf_key = rf_cfg.get("api_key", "")
    if rf_key and not ctx.params.get("skip_roboflow"):
        cmd += ["--roboflow-key", rf_key]
    else:
        cmd.append("--skip-roboflow")

    if ctx.params.get("skip_orin"):
        cmd.append("--skip-orin")
    if ctx.params.get("skip_oid"):
        cmd.append("--skip-oid")
    if ctx.params.get("skip_training_uploads"):
        cmd.append("--skip-training-uploads")

    oid_cfg = sources.get("open_images", {})
    cmd += ["--max-oid-per-class", str(ctx.params.get("max_oid_per_class", oid_cfg.get("max_per_class", 1500)))]
    cmd += ["--oid-workers", str(oid_cfg.get("workers", 16))]

    return _run_subprocess(ctx, cmd, phase="prepare_dataset")


_SECRET_ARGS = {"--roboflow-key"}


def _redact_cmd(cmd: list[str]) -> str:
    """Redact secret values from a command line for logging."""
    parts: list[str] = []
    skip_next = False
    for arg in cmd:
        if skip_next:
            parts.append("***")
            skip_next = False
        elif arg in _SECRET_ARGS:
            parts.append(arg)
            skip_next = True
        else:
            parts.append(arg)
    return " ".join(parts)


def _run_subprocess(ctx: JobContext, cmd: list[str], phase: str) -> dict:
    """Run a subprocess, streaming stdout/stderr to job log and Redis."""
    ctx.publish_progress(phase, 0, f"Starting {phase}")
    ctx.append_log(f"$ {_redact_cmd(cmd)}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    lines: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            ctx.append_log(line)

            epoch_info = _parse_epoch_progress(line)
            if epoch_info:
                ctx.publish_progress(phase, epoch_info["pct"], epoch_info["detail"], **epoch_info)

            if ctx.is_cancelled():
                proc.terminate()
                proc.wait(timeout=10)
                return {"error": "Job cancelled", "output_lines": len(lines)}

        proc.wait()
    except Exception:
        proc.kill()
        raise

    if proc.returncode != 0:
        last_lines = lines[-10:] if lines else ["(no output)"]
        raise RuntimeError(f"{phase} failed (exit {proc.returncode}): {' | '.join(last_lines)}")

    ctx.publish_progress(phase, 100, f"{phase} complete")
    return {"phase": phase, "exit_code": 0, "output_lines": len(lines)}


def _parse_epoch_progress(line: str) -> dict | None:
    """Try to extract epoch/mAP from ultralytics training output."""
    if "Epoch" not in line and "epoch" not in line.lower():
        return None
    try:
        parts = line.split()
        for i, p in enumerate(parts):
            if "/" in p:
                current, total = p.split("/")
                if current.isdigit() and total.isdigit():
                    pct = (int(current) / int(total)) * 100
                    return {"pct": pct, "detail": line.strip(), "epoch": int(current), "total_epochs": int(total)}
    except (ValueError, IndexError):
        pass
    return None


# ── train ────────────────────────────────────────────────────────────────────


def _run_train(ctx: JobContext) -> dict:
    train_cfg = ctx.training_config()
    defaults = train_cfg.get("defaults", {})
    dataset_dir = WORKSPACE_DIR / "merged_dataset"
    data_yaml = dataset_dir / "data.yaml"

    if not data_yaml.exists():
        return {"error": f"Dataset not found at {data_yaml} — run prepare_dataset first"}

    output_name = ctx.params.get("output_name", "trained.pt")
    output_path = Path(MODELS_DIR) / output_name

    cmd = [
        "python3", str(SCRIPTS_DIR / "train.py"),
        "--data", str(data_yaml),
        "--base-model", ctx.params.get("base_model", defaults.get("base_model", "yolov8n.pt")),
        "--output", str(output_path),
        "--epochs", str(ctx.params.get("epochs", defaults.get("epochs", 100))),
        "--imgsz", str(ctx.params.get("image_size", defaults.get("image_size", 480))),
        "--batch", str(ctx.params.get("batch_size", defaults.get("batch_size", 2))),
        "--patience", str(ctx.params.get("patience", defaults.get("patience", 20))),
        "--device", "0",
    ]

    if ctx.params.get("force"):
        cmd.append("--force")

    ctx.publish_progress("pausing", 0, "Pausing detector for GPU access")
    if not ctx.pause_detector():
        return {"error": "Failed to pause detector — timeout waiting for ack"}

    try:
        result = _run_subprocess(ctx, cmd, phase="train")
        result["model_path"] = str(output_path)
        return result
    finally:
        ctx.resume_detector()


# ── prepare_and_train ────────────────────────────────────────────────────────


def _run_prepare_and_train(ctx: JobContext) -> dict:
    prep_result = _run_prepare_dataset(ctx)
    if "error" in prep_result:
        return prep_result
    if ctx.is_cancelled():
        return {"error": "Job cancelled after prepare_dataset"}
    train_result = _run_train(ctx)
    return {"prepare": prep_result, "train": train_result}
