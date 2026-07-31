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
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

from detector_controller import DetectorControllerClient
from pause_protocol import HEARTBEAT_INTERVAL, HEARTBEAT_KEY, HEARTBEAT_TTL
from redis_client import make_sync_client
from training_safety import ORIN_DEFAULT_WORKERS, validate_orin_workers, validate_resume_checkpoint

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/scarguard.db")
MODELS_DIR = os.environ.get("MODELS_DIR", "/models")
TRAINING_UPLOADS_DIR = os.environ.get("TRAINING_UPLOADS_DIR", "/data/training_uploads")
WORKSPACE_DIR = Path(os.environ.get("TRAINING_WORKSPACE_DIR", "/data/training_workspace"))
SCRIPTS_DIR = Path("/app/scripts")

_PROGRESS_TTL = 300
_LOG_TTL = 3600
_LOG_CAP = 500
_TAIL_CAP = 200
_DEFAULT_LOG_MAX_BYTES = 16 * 1024 * 1024
_DEFAULT_LOG_RETENTION_DAYS = 30
_DEFAULT_MIN_MEM_AVAILABLE_MB = 1536
_DEFAULT_MIN_SWAP_FREE_MB = 512
_RESOURCE_SAMPLE_SECONDS = 2.0
_OUTPUT_READ_CHARS = 16_000
_TEXT_LIMIT_CHARS = 16_384
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TRACEBACK_START = "Traceback (most recent call last):"

# Mirrors the web form's class-name validation (routes/training_jobs.py).
_CLASS_TOKEN_RE = re.compile(r"^[a-z0-9_-]+$")
_JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def _progress_key(job_id: str) -> str:
    return f"scarguard:training:job:{job_id}:progress"


def _log_key(job_id: str) -> str:
    return f"scarguard:training:job:{job_id}:log"


def _log_seq_key(job_id: str) -> str:
    return f"scarguard:training:job:{job_id}:log-seq"


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
        if not _JOB_ID_RE.fullmatch(self.job_id):
            raise ValueError("Training job id must be 32 lowercase hexadecimal characters")
        self.job_type: str = job["type"]
        self.params: dict = (
            json.loads(job["params"]) if isinstance(job["params"], str) else job["params"]
        )
        self.cfg = cfg
        self.stop_event = stop_event
        self.redis_cfg = cfg.get("redis", {})
        self._redis = make_sync_client(self.redis_cfg)
        self._detector_controller = DetectorControllerClient(self.job_id)
        self._lease_stop = threading.Event()
        self._lease_thread: threading.Thread | None = None
        self._lease_acquired = False
        self._lease_lost = threading.Event()
        self._secrets = _collect_secret_values(cfg)
        self._secrets.extend(
            value
            for value in (
                os.environ.get("REDIS_PASSWORD", ""),
                os.environ.get("TRAINING_CONTROLLER_TOKEN", ""),
            )
            if len(value) >= 4
        )

        logs_cfg = self.training_config().get("logs", {})
        self.log_max_bytes = max(64 * 1024, int(logs_cfg.get("max_bytes", _DEFAULT_LOG_MAX_BYTES)))
        self.log_retention_days = max(
            1, int(logs_cfg.get("retention_days", _DEFAULT_LOG_RETENTION_DAYS))
        )
        self.log_path = WORKSPACE_DIR / "logs" / f"{self.job_id}.log"
        if WORKSPACE_DIR.is_symlink():
            raise RuntimeError("Training workspace must not be a symlink")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.log_path.parent.is_symlink():
            raise RuntimeError("Training log directory must not be a symlink")
        _prune_durable_logs(self.log_path.parent, self.log_retention_days)
        descriptor = os.open(
            self.log_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        descriptor_stat = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            os.close(descriptor)
            raise RuntimeError("Training durable log must be a regular file")
        os.fchmod(descriptor, 0o600)
        self._log_file = os.fdopen(descriptor, "a", encoding="utf-8", buffering=1)
        self._durable_bytes = descriptor_stat.st_size
        self._durable_truncated = self._durable_bytes >= self.log_max_bytes

    def close(self) -> None:
        if self._lease_acquired:
            try:
                self.release_detector()
            except Exception:
                logger.exception("Detector release failed while closing job %s", self.job_id)
        try:
            self._log_file.close()
        except Exception:
            pass
        try:
            self._redis.close()
        except Exception:
            pass

    def publish_progress(self, phase: str, pct: float, detail: str = "", **extra: Any) -> None:
        data = {"phase": phase, "pct": round(pct, 1), "detail": detail, **extra}
        try:
            self._redis.set(_progress_key(self.job_id), json.dumps(data), ex=_PROGRESS_TTL)
        except Exception:
            # Redis is the capped live view, never the durable execution log.
            logger.warning("Live progress publication failed for job %s", self.job_id)

    def append_log(self, line: str) -> None:
        line = self.sanitize(line)
        encoded = (line + "\n").encode("utf-8", errors="replace")
        if not self._durable_truncated:
            remaining = self.log_max_bytes - self._durable_bytes
            if len(encoded) <= remaining:
                self._log_file.write(encoded.decode("utf-8", errors="replace"))
                self._durable_bytes += len(encoded)
            else:
                marker = b"\n[durable log truncated at configured byte limit]\n"
                writable = max(0, remaining - len(marker))
                if writable:
                    self._log_file.write(encoded[:writable].decode("utf-8", errors="ignore"))
                if remaining >= len(marker):
                    self._log_file.write(marker.decode())
                self._log_file.flush()
                self._durable_bytes = self.log_max_bytes
                self._durable_truncated = True
        try:
            key = _log_key(self.job_id)
            self._redis.rpush(key, line)
            self._redis.ltrim(key, -_LOG_CAP, -1)
            self._redis.expire(key, _LOG_TTL)
            seq_key = _log_seq_key(self.job_id)
            self._redis.incr(seq_key)
            self._redis.expire(seq_key, _LOG_TTL)
        except Exception:
            logger.warning("Redis live log tail unavailable for job %s", self.job_id)

    def is_cancelled(self) -> bool:
        if self._lease_lost.is_set() or self.stop_event.is_set():
            return True
        try:
            return bool(self._redis.exists(_cancel_key(self.job_id)))
        except Exception:
            logger.warning("Could not poll cancellation state for job %s", self.job_id)
            return False

    def gpu_lease_lost(self) -> bool:
        return self._lease_lost.is_set()

    def sanitize(self, text: str) -> str:
        sanitized = _CONTROL_RE.sub("", _ANSI_RE.sub("", str(text)))
        for secret in self._secrets:
            sanitized = sanitized.replace(secret, "***")
        marker = " …[text truncated]"
        if len(sanitized) > _TEXT_LIMIT_CHARS:
            return sanitized[: _TEXT_LIMIT_CHARS - len(marker)] + marker
        return sanitized

    def acquire_detector(self) -> dict[str, Any]:
        """Atomically claim the shared GPU lease, then hard-stop the detector."""
        claimed = self._redis.set(HEARTBEAT_KEY, self.job_id, nx=True, ex=HEARTBEAT_TTL)
        if not claimed:
            holder = self._redis.get(HEARTBEAT_KEY)
            raise RuntimeError(f"GPU lease is held by {self.sanitize(str(holder or 'unknown'))}")
        try:
            state = self._detector_controller.acquire()
        except Exception:
            self._delete_gpu_lease_if_owned()
            raise
        self._lease_acquired = True
        self._lease_lost.clear()
        self._lease_stop.clear()
        self._lease_thread = threading.Thread(
            target=self._gpu_lease_heartbeat,
            name=f"gpu-lease-{self.job_id[:8]}",
            daemon=True,
        )
        self._lease_thread.start()
        return state

    def release_detector(self) -> dict[str, Any]:
        """Restore only a detector this job's controller lease stopped."""
        self._lease_stop.set()
        if self._lease_thread:
            self._lease_thread.join(timeout=5)
            self._lease_thread = None
        try:
            state = self._detector_controller.release()
        finally:
            self._delete_gpu_lease_if_owned()
            self._lease_acquired = False
        return state

    def _gpu_lease_heartbeat(self) -> None:
        while not self._lease_stop.wait(HEARTBEAT_INTERVAL):
            if not self._refresh_gpu_lease():
                self._lease_lost.set()
                logger.error(
                    "GPU lease refresh failed or ownership was lost for job %s; "
                    "terminating the training process group",
                    self.job_id,
                )
                return

    def _refresh_gpu_lease(self) -> bool:
        """Atomically extend only this job's lease, failing closed on Redis errors."""
        try:
            refreshed = self._redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end",
                1,
                HEARTBEAT_KEY,
                self.job_id,
                HEARTBEAT_TTL,
            )
            return bool(refreshed)
        except Exception:
            logger.exception("GPU lease heartbeat failed for job %s", self.job_id)
            return False

    def _delete_gpu_lease_if_owned(self) -> None:
        try:
            self._redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                HEARTBEAT_KEY,
                self.job_id,
            )
        except Exception:
            logger.exception("Failed to release Redis GPU lease for job %s", self.job_id)

    def gpu_lease_holder(self) -> str | None:
        holder = self._redis.get(HEARTBEAT_KEY)
        return self.sanitize(str(holder)) if holder else None

    def persist_execution(self, execution: dict[str, Any]) -> None:
        conn = _connect_db()
        try:
            conn.execute(
                "UPDATE training_jobs SET execution_metadata = ? WHERE id = ?",
                (json.dumps(execution), self.job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def training_config(self) -> dict:
        return self.cfg.get("training", {})

    def detection_config(self) -> dict:
        return self.cfg.get("detection", {})


def _collect_secret_values(value: object, key: str = "") -> list[str]:
    secrets: list[str] = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            secrets.extend(_collect_secret_values(child, str(child_key).lower()))
    elif isinstance(value, list):
        for child in value:
            secrets.extend(_collect_secret_values(child, key))
    elif any(token in key for token in ("password", "secret", "token", "api_key")):
        rendered = str(value)
        if len(rendered) >= 4:
            secrets.append(rendered)
    return secrets


def _prune_durable_logs(log_dir: Path, retention_days: int) -> None:
    cutoff = time.time() - retention_days * 86400
    for path in log_dir.glob("*.log"):
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            logger.warning("Could not prune expired training log %s", path)


def run_job(job: dict, cfg: dict, stop_event: threading.Event) -> dict:
    """Dispatch a job by type. Returns result dict."""
    ctx = JobContext(job, cfg, stop_event)
    try:
        if ctx.job_type == "process_video":
            result = _run_process_video(ctx)
        elif ctx.job_type == "prepare_dataset":
            result = _run_prepare_dataset(ctx)
        elif ctx.job_type == "train":
            result = _run_train(ctx)
        elif ctx.job_type == "prepare_and_train":
            result = _run_prepare_and_train(ctx)
        else:
            result = {"error": f"Unknown job type: {ctx.job_type}"}
        result.setdefault("log_path", str(ctx.log_path))
        return result
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

    ctx.publish_progress("isolating", 0, "Stopping detector process for exclusive GPU access")
    try:
        controller_state = ctx.acquire_detector()
    except RuntimeError as exc:
        return {"error": str(exc), "log_path": str(ctx.log_path)}

    try:
        results = []
        for i, uid in enumerate(upload_ids):
            if ctx.is_cancelled():
                return {"error": "Job cancelled", "processed": i}

            conn = _connect_db()
            try:
                upload = conn.execute(
                    "SELECT * FROM training_uploads WHERE id = ?", (uid,)
                ).fetchone()
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
            per_conf = (
                upload["confidence_threshold"] if "confidence_threshold" in upload_keys else None
            )
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
                uid,
                "processed",
                frame_count=result.frame_count,
                detection_count=result.deduped_detection_count,
            )
            results.append(
                {
                    "upload_id": uid,
                    "frames": result.frame_count,
                    "detections": result.deduped_detection_count,
                }
            )

        ctx.publish_progress("complete", 100, f"Processed {len(results)} upload(s)")
        return {"uploads": results}
    finally:
        try:
            restored = ctx.release_detector()
            ctx.append_log(
                "Detector lifecycle lease released "
                f"(stopped_by_controller={controller_state.get('stopped_by_controller')}, "
                f"restored={restored.get('restored')})"
            )
        except Exception:
            logger.exception("Detector release failed; controller recovery remains armed")


def _update_upload_status(
    upload_id: str,
    status: str,
    *,
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
        "python3",
        str(SCRIPTS_DIR / "prepare_dataset.py"),
        "--local-db",
        DB_PATH,
        "--local-snapshots",
        "/data/snapshots",
        "--training-uploads-db",
        DB_PATH,
        "--training-uploads-frames",
        TRAINING_UPLOADS_DIR,
        "--output",
        str(output_dir),
        "--val-split",
        str(ctx.params.get("val_split", defaults.get("val_split", 0.15))),
        "--seed",
        str(ctx.params.get("seed", 42)),
        "--background-sample-interval",
        str(
            ctx.params.get(
                "background_sample_interval",
                train_cfg.get("video", {}).get("background_sample_interval", 10),
            )
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
    roboflow_key_missing = False
    if rf_key and not ctx.params.get("skip_roboflow"):
        cmd += ["--roboflow-key", rf_key]
    else:
        cmd.append("--skip-roboflow")
        if not ctx.params.get("skip_roboflow"):
            roboflow_key_missing = True
            ctx.append_log(
                "WARNING: Roboflow source skipped — training.sources.roboflow.api_key "
                "is not set in scarguard.yml. Heron coverage depends on Roboflow; "
                "expect low annotation counts."
            )

    if ctx.params.get("skip_orin"):
        cmd.append("--skip-orin")
    if ctx.params.get("skip_oid"):
        cmd.append("--skip-oid")
    if ctx.params.get("skip_training_uploads"):
        cmd.append("--skip-training-uploads")

    oid_cfg = sources.get("open_images", {})
    cmd += [
        "--max-oid-per-class",
        str(ctx.params.get("max_oid_per_class", oid_cfg.get("max_per_class", 1500))),
    ]
    cmd += ["--oid-workers", str(oid_cfg.get("workers", 16))]

    result = _run_subprocess(ctx, cmd, phase="prepare_dataset")
    if roboflow_key_missing and "error" not in result:
        result["warnings"] = [
            "Roboflow source skipped: training.sources.roboflow.api_key is not configured"
        ]
    return result


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


def _read_meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in path.read_text().splitlines():
            key, _, raw = line.partition(":")
            token = raw.strip().split()[0]
            if token.isdigit():
                values[key] = int(token) * 1024
    except (OSError, IndexError):
        logger.exception("Unable to read host memory evidence from %s", path)
    return values


def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
        return int(raw) if raw != "max" else None
    except (OSError, ValueError):
        return None


def _read_events(path: Path) -> dict[str, int]:
    events: dict[str, int] = {}
    try:
        for line in path.read_text().splitlines():
            key, raw = line.split(maxsplit=1)
            events[key] = int(raw)
    except (OSError, ValueError):
        pass
    return events


def _resource_snapshot(
    ctx: JobContext, cgroup_root: Path = Path("/sys/fs/cgroup")
) -> dict[str, Any]:
    meminfo = _read_meminfo()
    current = _read_int(cgroup_root / "memory.current")
    peak = _read_int(cgroup_root / "memory.peak")
    events = _read_events(cgroup_root / "memory.events")
    if current is None:
        current = _read_int(cgroup_root / "memory" / "memory.usage_in_bytes")
    if peak is None:
        peak = _read_int(cgroup_root / "memory" / "memory.max_usage_in_bytes")
    if not events:
        fail_count = _read_int(cgroup_root / "memory" / "memory.failcnt")
        if fail_count is not None:
            events = {"failcnt": fail_count}
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mem_available_bytes": meminfo.get("MemAvailable"),
        "swap_free_bytes": meminfo.get("SwapFree"),
        "swap_total_bytes": meminfo.get("SwapTotal"),
        "cgroup_current_bytes": current,
        "cgroup_peak_bytes": peak,
        "cgroup_events": events,
        "gpu_lease_holder": ctx.gpu_lease_holder(),
    }


def _events_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {key: max(0, int(after.get(key, 0)) - int(value)) for key, value in before.items()} | {
        key: max(0, int(value) - int(before.get(key, 0)))
        for key, value in after.items()
        if key not in before
    }


class ResourceMonitor:
    """Sample unified host/cgroup memory without retaining an unbounded history."""

    def __init__(self, ctx: JobContext, initial: dict[str, Any]) -> None:
        self.ctx = ctx
        self.initial = initial
        self.latest = initial
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.min_mem_available = initial.get("mem_available_bytes")
        self.min_swap_free = initial.get("swap_free_bytes")
        self.max_cgroup_current = initial.get("cgroup_current_bytes")
        self.reported_peak_baseline = initial.get("cgroup_peak_bytes")
        self.reported_peak_final = initial.get("cgroup_peak_bytes")
        self.execution: dict[str, Any] | None = None
        self._last_persist = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="training-memory", daemon=True)
        self._thread.start()

    @staticmethod
    def _minimum(current: int | None, new: int | None) -> int | None:
        if current is None:
            return new
        if new is None:
            return current
        return min(current, new)

    @staticmethod
    def _maximum(current: int | None, new: int | None) -> int | None:
        if current is None:
            return new
        if new is None:
            return current
        return max(current, new)

    def _sample(self) -> None:
        try:
            snapshot = _resource_snapshot(self.ctx)
        except Exception:
            logger.exception("Training resource sample failed")
            return
        self.latest = snapshot
        self.min_mem_available = self._minimum(
            self.min_mem_available, snapshot.get("mem_available_bytes")
        )
        self.min_swap_free = self._minimum(self.min_swap_free, snapshot.get("swap_free_bytes"))
        self.max_cgroup_current = self._maximum(
            self.max_cgroup_current, snapshot.get("cgroup_current_bytes")
        )
        self.reported_peak_final = snapshot.get("cgroup_peak_bytes")
        if self.execution is not None and time.monotonic() - self._last_persist >= 30:
            self.execution["running_evidence"] = {
                "last_sample": snapshot,
                "min_mem_available_bytes": self.min_mem_available,
                "min_swap_free_bytes": self.min_swap_free,
                "cgroup_current_peak_bytes": self.max_cgroup_current,
                "cgroup_reported_peak_baseline_bytes": self.reported_peak_baseline,
                "cgroup_reported_peak_final_bytes": self.reported_peak_final,
            }
            try:
                self.ctx.persist_execution(self.execution)
            except Exception:
                logger.exception("Could not persist running training evidence")
            self._last_persist = time.monotonic()

    def _loop(self) -> None:
        while not self._stop.wait(_RESOURCE_SAMPLE_SECONDS):
            self._sample()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._sample()
        event_delta = _events_delta(
            self.initial.get("cgroup_events", {}), self.latest.get("cgroup_events", {})
        )
        return {
            "last_sample": self.latest,
            "min_mem_available_bytes": self.min_mem_available,
            "min_swap_free_bytes": self.min_swap_free,
            "cgroup_current_peak_bytes": self.max_cgroup_current,
            "cgroup_reported_peak_baseline_bytes": self.reported_peak_baseline,
            "cgroup_reported_peak_final_bytes": self.reported_peak_final,
            "cgroup_event_delta": event_delta,
        }


def _decode_return_code(return_code: int) -> tuple[int | None, str | None]:
    if return_code < 0:
        signal_number = -return_code
        try:
            return None, signal.Signals(signal_number).name
        except ValueError:
            return None, f"SIGNAL_{signal_number}"
    return return_code, None


def _traceback_block(lines: deque[str]) -> str | None:
    material = list(lines)
    starts = [index for index, line in enumerate(material) if line.startswith(_TRACEBACK_START)]
    if starts:
        return "\n".join(material[starts[-1] :])
    return None


def _final_exception(lines: deque[str]) -> str | None:
    for line in reversed(lines):
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _version_identity() -> dict[str, str]:
    versions = {
        "python": sys.version.split()[0],
        "image": os.environ.get("SCARGUARD_IMAGE", "scarguard-trainer"),
        "image_version": os.environ.get("VERSION", "unknown"),
        "image_revision": os.environ.get("GIT_COMMIT", "unknown"),
        "base_image": os.environ.get("SCARGUARD_BASE_IMAGE", "dustynv/l4t-pytorch:r36.4.0"),
        "cuda": os.environ.get("SCARGUARD_CUDA_VERSION", "12.6"),
        "expected_torch": os.environ.get("SCARGUARD_TORCH_VERSION", "2.4.0"),
    }
    for package in (
        "torch",
        "torchvision",
        "ultralytics",
        "opencv-python-headless",
        "roboflow",
    ):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    return versions


def _probable_oom(signal_name: str | None, running: dict[str, Any]) -> tuple[bool, list[str]]:
    if signal_name != "SIGKILL":
        return False, []
    event_delta = running.get("cgroup_event_delta", {})
    reasons: list[str] = []
    if int(event_delta.get("oom_kill", 0)) > 0 or int(event_delta.get("oom", 0)) > 0:
        reasons.append("cgroup memory.events recorded an OOM delta")
    min_mem = running.get("min_mem_available_bytes")
    min_swap = running.get("min_swap_free_bytes")
    swap_total = running.get("last_sample", {}).get("swap_total_bytes")
    if (
        isinstance(min_mem, int)
        and min_mem <= 256 * 1024 * 1024
        and isinstance(min_swap, int)
        and min_swap <= 64 * 1024 * 1024
        and isinstance(swap_total, int)
        and swap_total > 0
    ):
        reasons.append("host MemAvailable and swap free were both exhausted")
    return bool(reasons), reasons


def _terminate_process_group(proc: subprocess.Popen[str], timeout: float = 10.0) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        proc.poll()  # Reap an exited leader so it does not keep the PGID alive.
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    try:
        os.killpg(proc.pid, 0)
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if proc.poll() is None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.error("Process-group leader %s did not exit after SIGKILL", proc.pid)


def _run_subprocess(
    ctx: JobContext,
    cmd: list[str],
    phase: str,
    *,
    preflight: dict[str, Any] | None = None,
) -> dict:
    """Run a process group with bounded memory, complete bounded durable logs, and evidence."""
    ctx.publish_progress(phase, 0, f"Starting {phase}")
    ctx.append_log(f"$ {_redact_cmd(cmd)}")

    # Run from the writable workspace: /app is read-only for the service
    # user, and ultralytics resolves relative output paths and asset
    # downloads against the process cwd.
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()
    baseline = preflight or _resource_snapshot(ctx)
    monitor = ResourceMonitor(ctx, baseline)
    execution: dict[str, Any] = {
        "command": ctx.sanitize(_redact_cmd(cmd)),
        "cwd": str(WORKSPACE_DIR),
        "versions": _version_identity(),
        "started_at": started_at,
        "log_path": str(ctx.log_path),
        "preflight": baseline,
    }
    monitor.execution = execution
    ctx.persist_execution(execution)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(WORKSPACE_DIR),
            start_new_session=True,
        )
    except OSError as exc:
        execution.update(
            {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "return_code": None,
                "signal": None,
                "final_exception": f"{type(exc).__name__}: {exc}",
            }
        )
        ctx.persist_execution(execution)
        return {"error": f"Could not start {phase}: {exc}", "execution": execution}

    lines: deque[str] = deque(maxlen=_TAIL_CAP)
    line_count = 0
    cancelled = threading.Event()
    cancel_watch_stop = threading.Event()

    def _watch_cancellation() -> None:
        while not cancel_watch_stop.wait(0.5):
            if ctx.is_cancelled():
                cancelled.set()
                _terminate_process_group(proc)
                return
            # A loader descendant can inherit stdout after its leader exits.
            # Reap the whole isolated group so the output reader reaches EOF.
            if proc.poll() is not None:
                _terminate_process_group(proc, timeout=1)
                return

    cancel_thread = threading.Thread(
        target=_watch_cancellation,
        name=f"cancel-{ctx.job_id[:8] if hasattr(ctx, 'job_id') else phase}",
        daemon=True,
    )
    monitor.start()
    cancel_thread.start()
    try:
        assert proc.stdout is not None
        while True:
            raw_line = proc.stdout.readline(_OUTPUT_READ_CHARS)
            if raw_line == "":
                break
            continued = len(raw_line) == _OUTPUT_READ_CHARS and not raw_line.endswith("\n")
            line = ctx.sanitize(raw_line.rstrip("\n"))
            if continued:
                line += " …[line continues in next log record]"
            lines.append(line)
            line_count += 1
            ctx.append_log(line)

            epoch_info = _parse_epoch_progress(line)
            if epoch_info:
                ctx.publish_progress(phase, epoch_info["pct"], epoch_info["detail"], **epoch_info)

        if proc.poll() is None:
            proc.wait()
    except Exception:
        _terminate_process_group(proc, timeout=2)
        raise
    finally:
        cancel_watch_stop.set()
        cancel_thread.join(timeout=2)
        running = monitor.stop()

    return_code = int(proc.returncode if proc.returncode is not None else -signal.SIGKILL)
    exit_code, signal_name = _decode_return_code(return_code)
    traceback = _traceback_block(lines)
    last_output_line = _final_exception(lines)
    probable_oom, oom_evidence = _probable_oom(signal_name, running)
    combined_error_text = "\n".join(lines)
    nvml_masked = (
        "NVML_SUCCESS == r INTERNAL ASSERT FAILED" in combined_error_text
        and "CUDACachingAllocator.cpp" in combined_error_text
    )
    cuda_allocation_failure = "CUDA out of memory" in combined_error_text or nvml_masked

    execution.update(
        {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "return_code": return_code,
            "exit_code": exit_code,
            "signal": signal_name,
            "line_count": line_count,
            "tail": list(lines),
            "final_traceback": traceback,
            "last_output_line": last_output_line,
            "final_exception": (
                f"{phase} terminated by {signal_name}" if signal_name else last_output_line
            ),
            # memory.peak is cumulative on the deployed cgroup and cannot be
            # reset safely here. The per-job peak is the maximum sampled
            # current value; reported baseline/final peaks remain evidence.
            "memory_peak_bytes": running.get("cgroup_current_peak_bytes"),
            "cgroup_event_delta": running.get("cgroup_event_delta", {}),
            "resources": running,
            "probable_oom": probable_oom,
            "oom_evidence": oom_evidence,
            "cuda_allocation_failure": cuda_allocation_failure,
            "diagnostic_masking": "jetson_nvml_process_query" if nvml_masked else None,
        }
    )
    ctx.persist_execution(execution)

    if cancelled.is_set():
        if getattr(ctx, "gpu_lease_lost", lambda: False)():
            return {
                "error": "GPU lease ownership was lost; training was stopped",
                "execution": execution,
            }
        return {"error": "Job cancelled", "cancelled": True, "execution": execution}

    if return_code != 0:
        if signal_name:
            error = f"{phase} terminated by {signal_name}"
        elif last_output_line:
            error = last_output_line
        else:
            error = f"{phase} failed with exit code {exit_code} and no output"
        return {"error": error, "execution": execution}

    ctx.publish_progress(phase, 100, f"{phase} complete")
    return {
        "phase": phase,
        "exit_code": 0,
        "output_lines": line_count,
        "log_path": str(ctx.log_path),
        "execution": execution,
    }


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
                    return {
                        "pct": pct,
                        "detail": line.strip(),
                        "epoch": int(current),
                        "total_epochs": int(total),
                    }
    except (ValueError, IndexError):
        pass
    return None


# ── train ────────────────────────────────────────────────────────────────────


def _run_train(ctx: JobContext) -> dict:
    started_epoch = time.time()
    train_cfg = ctx.training_config()
    defaults = train_cfg.get("defaults", {})
    dataset_dir = WORKSPACE_DIR / "merged_dataset"
    data_yaml = dataset_dir / "data.yaml"

    if not data_yaml.exists():
        return {"error": f"Dataset not found at {data_yaml} — run prepare_dataset first"}

    output_name = Path(str(ctx.params.get("output_name", "trained.pt"))).name
    output_path = Path(MODELS_DIR) / output_name

    try:
        workers = validate_orin_workers(
            ctx.params.get("workers", defaults.get("workers", ORIN_DEFAULT_WORKERS))
        )
    except ValueError as exc:
        return {"error": str(exc), "log_path": str(ctx.log_path)}

    resume_from: Path | None = None
    if ctx.params.get("resume_from"):
        try:
            resume_from = validate_resume_checkpoint(
                ctx.params["resume_from"], WORKSPACE_DIR / "runs"
            )
        except (ValueError, OSError) as exc:
            return {"error": f"Invalid resume checkpoint: {exc}", "log_path": str(ctx.log_path)}

    # Bare checkpoint names resolve against MODELS_DIR when staged there,
    # so a local yolov8n.pt is used instead of a GitHub download.
    base_model = str(
        ctx.params.get("base_model", defaults.get("base_model", "yolov8n.pt")) or "yolov8n.pt"
    )
    if "/" not in base_model:
        staged = Path(MODELS_DIR) / base_model
        if staged.is_file():
            base_model = str(staged)

    cmd = [
        "python3",
        str(SCRIPTS_DIR / "train.py"),
        "--data",
        str(data_yaml),
        "--base-model",
        base_model,
        "--output",
        str(output_path),
        "--project",
        str(WORKSPACE_DIR / "runs"),
        "--epochs",
        str(ctx.params.get("epochs", defaults.get("epochs", 100))),
        "--imgsz",
        str(ctx.params.get("image_size", defaults.get("image_size", 480))),
        "--batch",
        str(ctx.params.get("batch_size", defaults.get("batch_size", 2))),
        "--patience",
        str(ctx.params.get("patience", defaults.get("patience", 20))),
        "--device",
        "0",
        "--workers",
        str(workers),
    ]

    if resume_from:
        cmd += ["--resume-from", str(resume_from)]

    if ctx.params.get("force"):
        cmd.append("--force")

    ctx.publish_progress("isolating", 0, "Stopping detector process for exclusive GPU access")
    try:
        controller_state = ctx.acquire_detector()
    except RuntimeError as exc:
        return {"error": str(exc), "log_path": str(ctx.log_path)}

    try:
        preflight = _resource_snapshot(ctx)
        resources_cfg = train_cfg.get("resources", {})
        min_mem_mb = int(resources_cfg.get("min_mem_available_mb", _DEFAULT_MIN_MEM_AVAILABLE_MB))
        min_swap_mb = int(resources_cfg.get("min_swap_free_mb", _DEFAULT_MIN_SWAP_FREE_MB))
        admission: dict[str, Any] = {
            "preflight": preflight,
            "thresholds": {
                "min_mem_available_bytes": min_mem_mb * 1024 * 1024,
                "min_swap_free_bytes": min_swap_mb * 1024 * 1024,
            },
            "detector_lease": controller_state,
            "log_path": str(ctx.log_path),
        }
        ctx.persist_execution(admission)

        mem_available = preflight.get("mem_available_bytes")
        swap_free = preflight.get("swap_free_bytes")
        swap_total = preflight.get("swap_total_bytes")
        rejection_reasons: list[str] = []
        if not isinstance(mem_available, int) or mem_available < min_mem_mb * 1024 * 1024:
            rejection_reasons.append(f"MemAvailable is below {min_mem_mb} MiB")
        if (
            isinstance(swap_total, int)
            and swap_total > 0
            and (not isinstance(swap_free, int) or swap_free < min_swap_mb * 1024 * 1024)
        ):
            rejection_reasons.append(f"swap free is below {min_swap_mb} MiB")
        if rejection_reasons:
            admission["admitted"] = False
            admission["rejection_reasons"] = rejection_reasons
            ctx.persist_execution(admission)
            return {
                "error": "Training admission rejected: " + "; ".join(rejection_reasons),
                "execution": admission,
                "log_path": str(ctx.log_path),
            }

        admission["admitted"] = True
        ctx.persist_execution(admission)
        result = _run_subprocess(ctx, cmd, phase="train", preflight=preflight)
        if "error" not in result:
            result["model_path"] = str(output_path)
        if resume_from:
            result["resume_from"] = str(resume_from)
            result["run_dir"] = str(resume_from.parent.parent)
        # A resume may fail before touching last.pt. The already-validated
        # source checkpoint remains a deliberate retry candidate.
        checkpoint = _newest_checkpoint_since(started_epoch) or resume_from
        if checkpoint:
            result["checkpoint_path"] = str(checkpoint)
            if isinstance(result.get("execution"), dict):
                result["execution"]["checkpoint_path"] = str(checkpoint)
        return result
    finally:
        try:
            restored = ctx.release_detector()
            ctx.append_log(
                "Detector lifecycle lease released "
                f"(stopped_by_controller={controller_state.get('stopped_by_controller')}, "
                f"restored={restored.get('restored')})"
            )
        except Exception:
            # The controller owns durable recovery and will retry after the
            # trainer heartbeat expires. Preserve the training diagnostic.
            logger.exception("Detector release failed; controller recovery remains armed")


# ── prepare_and_train ────────────────────────────────────────────────────────


def _newest_checkpoint_since(started_epoch: float) -> Path | None:
    runs_root = WORKSPACE_DIR / "runs"
    if not runs_root.is_dir():
        return None
    candidates: list[Path] = []
    for path in runs_root.glob("*/weights/last.pt"):
        try:
            valid = validate_resume_checkpoint(path, runs_root)
            if valid.stat().st_mtime >= started_epoch - 5:
                candidates.append(valid)
        except (OSError, ValueError):
            continue
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _run_prepare_and_train(ctx: JobContext) -> dict:
    prep_result = _run_prepare_dataset(ctx)
    if "error" in prep_result:
        return prep_result
    if ctx.is_cancelled():
        return {"error": "Job cancelled after prepare_dataset"}
    train_result = _run_train(ctx)
    result: dict[str, Any] = {"prepare": prep_result, "train": train_result}
    if "error" in train_result:
        result["error"] = train_result["error"]
    if train_result.get("cancelled"):
        result["cancelled"] = True
    return result
