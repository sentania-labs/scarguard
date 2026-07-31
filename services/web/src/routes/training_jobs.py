"""Training job submission, monitoring, and SSE progress endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

import db as db_module
import redis.asyncio as aioredis
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from redis_client import make_sync_client
from route_auth import require_admin, require_viewer
from sse_limiter import SSETooManyStreams, sse_connection
from starlette.responses import Response, StreamingResponse
from training_safety import ORIN_DEFAULT_WORKERS, validate_orin_workers, validate_resume_checkpoint

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/training/jobs")
templates = Jinja2Templates(
    directory=str(__import__("pathlib").Path(__file__).parent.parent / "templates")
)

JOB_NOTIFY_CHANNEL = "scarguard:training:job:notify"
STATE_KEY = "scarguard:detector:state"
PAGE_SIZE = 25
WORKSPACE_DIR = Path(os.environ.get("TRAINING_WORKSPACE_DIR", "/data/training_workspace"))
RUNS_DIR = WORKSPACE_DIR / "runs"
LOGS_DIR = WORKSPACE_DIR / "logs"

_VALID_JOB_TYPES = {"process_video", "prepare_dataset", "train", "prepare_and_train"}

# Default training class list. Keep in sync with DEFAULT_CLASSES in
# training/prepare_dataset.py (standalone by design — copied into the
# trainer image, so it can't be imported here). person/dog/cat/plant are
# distractor classes: trained so the model stops calling them herons,
# filtered at runtime via detection.target_classes.
DEFAULT_TRAINING_CLASSES = "duck,heron,raccoon,person,dog,cat,plant"

_CLASS_TOKEN_RE = __import__("re").compile(r"^[a-z0-9_-]+$")

_CLASSES_JOB_TYPES = {"prepare_dataset", "prepare_and_train"}


def training_classes_default() -> str:
    """Resolve the Classes prefill: training.defaults.classes or built-in."""
    import config_store

    cfg = config_store.load_cached()
    classes = cfg.get("training", {}).get("defaults", {}).get("classes")
    if isinstance(classes, (list, tuple)) and classes:
        return ",".join(str(c).strip() for c in classes)
    if isinstance(classes, str) and classes.strip():
        return classes.strip()
    return DEFAULT_TRAINING_CLASSES


def training_workers_default() -> int:
    """Return a validated Orin worker default even when legacy config is malformed."""
    import config_store

    raw = config_store.load_cached().get("training", {}).get("defaults", {}).get("workers", 4)
    try:
        return validate_orin_workers(raw)
    except ValueError:
        return ORIN_DEFAULT_WORKERS


def _redis_cfg() -> dict:
    import config_store

    cfg = config_store.load_cached()
    return cfg.get("redis", {})


# ── Jobs list page ───────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def jobs_page(
    request: Request,
    page: int = 1,
    status: str = "",
) -> Response:
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate
    st = status or None
    offset = (page - 1) * PAGE_SIZE
    jobs = db_module.get_training_jobs(limit=PAGE_SIZE, offset=offset, status=st)
    total = db_module.count_training_jobs(status=st)

    detector_state = _get_detector_state()

    job_list = []
    newest_resume = _newest_failed_resume_candidate()
    for j in jobs:
        d = dict(j)
        d["parsed_result"] = _parse_result(d.get("result"))
        d["result_view"] = _result_view(d["parsed_result"], d.get("execution_metadata"))
        if newest_resume and newest_resume[0] == d["id"]:
            d["resume_candidate"] = str(newest_resume[1])
        job_list.append(d)

    # Pull uploads available for the Process Videos picker (any state — user can
    # re-process processed ones with clear_existing_events).
    upload_rows = db_module.get_training_uploads(limit=200)
    upload_options = [
        {
            "id": u["id"],
            "filename": u["filename"],
            "status": u["status"],
            "detection_count": u["detection_count"],
        }
        for u in upload_rows
    ]

    return templates.TemplateResponse(
        request,
        "training_jobs.html",
        {
            "jobs": job_list,
            "total": total,
            "page": page,
            "total_pages": max(1, -(-total // PAGE_SIZE)),
            "filter_status": status,
            "detector_state": detector_state,
            "is_admin": require_admin(request)
            if not isinstance(require_admin(request), dict)
            else True,
            "upload_options": upload_options,
            "training_classes_default": training_classes_default(),
            "training_workers_default": training_workers_default(),
        },
    )


# ── Submit job ───────────────────────────────────────────────────────────────


_UPLOAD_ID_RE = __import__("re").compile(r"^[0-9a-f]{32}$")


@router.post("")
async def submit_job(request: Request) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate

    form = await request.form()
    job_type = str(form.get("job_type") or "").strip()
    if job_type not in _VALID_JOB_TYPES:
        return JSONResponse({"error": f"Invalid job type: {job_type}"}, status_code=400)

    if job_type == "process_video":
        # Structured form: explicit upload picker + clear_existing checkbox.
        upload_ids_raw = form.getlist("upload_ids")
        upload_ids = [
            uid for uid in upload_ids_raw if isinstance(uid, str) and _UPLOAD_ID_RE.match(uid)
        ]
        clear_existing = bool(form.get("clear_existing_events"))
        params: dict = {}
        if upload_ids:
            params["upload_ids"] = upload_ids
        if clear_existing:
            params["clear_existing_events"] = True
    else:
        # Other job types still use the free-form JSON textarea.
        try:
            params = json.loads(str(form.get("params_json") or "{}"))
        except json.JSONDecodeError:
            return JSONResponse({"error": "Invalid JSON in params"}, status_code=400)
        if not isinstance(params, dict):
            return JSONResponse({"error": "Parameters JSON must be an object"}, status_code=400)

        # Visible Classes field (prepare jobs only); wins over a "classes"
        # key in the JSON textarea when filled.
        classes_raw = str(form.get("classes") or "").strip()
        if classes_raw and job_type in _CLASSES_JOB_TYPES:
            tokens: list[str] = []
            for part in classes_raw.lower().split(","):
                token = part.strip()
                if not token:
                    continue
                if not _CLASS_TOKEN_RE.match(token):
                    return JSONResponse(
                        {"error": f"Invalid class name: {token!r}"}, status_code=400
                    )
                if token not in tokens:
                    tokens.append(token)
            if tokens:
                params["classes"] = ",".join(tokens)

        if job_type in {"train", "prepare_and_train"}:
            try:
                params["workers"] = validate_orin_workers(
                    form.get("workers")
                    if form.get("workers") is not None
                    else params.get("workers", 4)
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            if job_type == "prepare_and_train" and params.get("resume_from"):
                return JSONResponse(
                    {"error": "Resume is train-only; select Resume checkpoint on a failed job"},
                    status_code=400,
                )
            if params.get("resume_from"):
                try:
                    params["resume_from"] = str(
                        validate_resume_checkpoint(params["resume_from"], RUNS_DIR)
                    )
                except (ValueError, OSError) as exc:
                    return JSONResponse(
                        {"error": f"Invalid resume checkpoint: {exc}"}, status_code=400
                    )

    running = db_module.count_training_jobs(status="running")
    queued = db_module.count_training_jobs(status="queued")
    if running > 0 or queued > 0:
        return JSONResponse(
            {"error": "A job is already running or queued. Wait for it to complete."},
            status_code=409,
        )

    job_id = uuid.uuid4().hex
    db_module.create_training_job(job_id, job_type, json.dumps(params))

    try:
        client = make_sync_client(_redis_cfg())
        client.publish(JOB_NOTIFY_CHANNEL, json.dumps({"job_id": job_id}))
        client.close()
    except Exception:
        logger.warning("Failed to notify trainer via Redis — trainer will pick up via poll")

    logger.info("Training job submitted: %s (type=%s)", job_id, job_type)
    from starlette.responses import RedirectResponse

    return RedirectResponse(url=f"/admin/training/jobs?submitted={job_id}", status_code=303)


@router.post("/{job_id}/resume")
async def resume_job(request: Request, job_id: str) -> Response:
    """Queue a deliberate train-only resume from this failed job's valid checkpoint."""
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    job = db_module.get_training_job(job_id)
    if not job or job["status"] != "failed":
        return JSONResponse({"error": "Only failed jobs can be resumed"}, status_code=400)

    candidate = _resume_candidate(dict(job))
    if candidate is None:
        return JSONResponse({"error": "No valid checkpoint is available"}, status_code=400)
    if db_module.count_training_jobs(status="running") or db_module.count_training_jobs(
        status="queued"
    ):
        return JSONResponse({"error": "A job is already running or queued"}, status_code=409)

    try:
        old_params = json.loads(job["params"] or "{}")
    except (json.JSONDecodeError, TypeError):
        old_params = {}
    params = old_params if isinstance(old_params, dict) else {}
    params["resume_from"] = str(candidate)
    try:
        params["workers"] = validate_orin_workers(params.get("workers", training_workers_default()))
    except ValueError:
        params["workers"] = training_workers_default()
    new_job_id = uuid.uuid4().hex
    db_module.create_training_job(new_job_id, "train", json.dumps(params))
    try:
        client = make_sync_client(_redis_cfg())
        client.publish(JOB_NOTIFY_CHANNEL, json.dumps({"job_id": new_job_id}))
        client.close()
    except Exception:
        logger.warning("Failed to notify trainer about resume job; poll fallback remains active")

    from starlette.responses import RedirectResponse

    return RedirectResponse(url=f"/admin/training/jobs?submitted={new_job_id}", status_code=303)


# ── Job detail ───────────────────────────────────────────────────────────────


@router.get("/{job_id}")
async def job_detail(request: Request, job_id: str) -> Response:
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate

    job = db_module.get_training_job(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)

    progress = None
    log_lines: list[str] = []
    try:
        client = make_sync_client(_redis_cfg())
        raw_progress: str | None = client.get(f"scarguard:training:job:{job_id}:progress")  # type: ignore[assignment,unused-ignore]
        if raw_progress:
            progress = json.loads(raw_progress)
        raw_log: list[str] = client.lrange(f"scarguard:training:job:{job_id}:log", -50, -1)  # type: ignore[assignment,unused-ignore]
        log_lines = raw_log if raw_log else []
        client.close()
    except Exception:
        pass

    return JSONResponse(
        {
            "job": dict(job),
            "progress": progress,
            "log": log_lines,
            "result_view": _result_view(
                _parse_result(job["result"]),
                job["execution_metadata"] if "execution_metadata" in job.keys() else None,
            ),
        }
    )


@router.get("/{job_id}/log")
async def durable_job_log(request: Request, job_id: str) -> Response:
    """Serve only the durable log path recorded for this job."""
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate
    job = db_module.get_training_job(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    view = _result_view(
        _parse_result(job["result"]),
        job["execution_metadata"] if "execution_metadata" in job.keys() else None,
    )
    raw_path = view.get("log_path")
    if not raw_path:
        return JSONResponse({"error": "No durable log recorded"}, status_code=404)
    try:
        root = LOGS_DIR.resolve(strict=True)
        path = Path(str(raw_path))
        if path.is_symlink():
            raise ValueError("symlink")
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            raise ValueError("not regular")
    except (OSError, ValueError):
        return JSONResponse({"error": "Durable log path is invalid"}, status_code=404)
    return FileResponse(
        resolved,
        media_type="text/plain; charset=utf-8",
        filename=f"training-{job_id}.log",
    )


# ── SSE progress stream ─────────────────────────────────────────────────────


@router.get("/{job_id}/stream")
async def job_stream(request: Request, job_id: str) -> Response:
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate

    user = getattr(request.state, "user", None) or {}
    user_id = user.get("user_id", "anon")
    redis_cfg = _redis_cfg()

    async def _stream():
        # Use the async Redis client so .get/.llen/.lrange don't block
        # the asyncio event loop — a sync client here was starving
        # neighbouring SSE generators (logs, deterrent-stuck) for the
        # duration of a job, breaking them with apparent "connection lost".
        client = aioredis.Redis(
            host=redis_cfg.get("host", "redis"),
            port=int(redis_cfg.get("port", 6379)),
            password=os.environ.get("REDIS_PASSWORD", "") or None,
            decode_responses=True,
        )
        progress_key = f"scarguard:training:job:{job_id}:progress"
        log_key = f"scarguard:training:job:{job_id}:log"
        log_seq_key = f"scarguard:training:job:{job_id}:log-seq"
        last_log_seq = 0
        max_iterations = 43200  # 12 hours at 1s intervals

        try:
            async with sse_connection(client, user_id):
                for _ in range(max_iterations):
                    if await request.is_disconnected():
                        return
                    raw = await client.get(progress_key)
                    if raw:
                        yield f"event: progress\ndata: {raw}\n\n"

                    current_log_len = int(await client.llen(log_key))
                    raw_seq = await client.get(log_seq_key)
                    current_log_seq = int(raw_seq) if raw_seq else current_log_len
                    first_retained_seq = max(1, current_log_seq - current_log_len + 1)
                    next_seq = max(last_log_seq + 1, first_retained_seq)
                    if current_log_seq >= next_seq:
                        first_index = next_seq - first_retained_seq
                        new_lines = await client.lrange(log_key, first_index, -1)
                        if new_lines:
                            for line in new_lines:
                                yield f"event: log\ndata: {json.dumps(line)}\n\n"
                        last_log_seq = current_log_seq

                    job = db_module.get_training_job(job_id)
                    if job and job["status"] in ("completed", "failed", "cancelled"):
                        result_data = json.loads(job["result"]) if job["result"] else {}
                        result_view = _result_view(
                            result_data,
                            job["execution_metadata"]
                            if "execution_metadata" in job.keys()
                            else None,
                        )
                        yield (
                            "event: result\ndata: "
                            + json.dumps(
                                {
                                    "status": job["status"],
                                    **result_data,
                                    "result_view": result_view,
                                }
                            )
                            + "\n\n"
                        )
                        break

                    await asyncio.sleep(1)
        except SSETooManyStreams:
            yield "event: error\ndata: Too many active streams\n\n"
        finally:
            try:
                await client.aclose()
            except Exception:
                pass

    return StreamingResponse(_stream(), media_type="text/event-stream")


# ── Cancel job ───────────────────────────────────────────────────────────────


@router.post("/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate

    job = db_module.get_training_job(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)

    if job["status"] == "queued":
        db_module.update_training_job_status(job_id, "cancelled")
    elif job["status"] == "running":
        try:
            client = make_sync_client(_redis_cfg())
            client.set(f"scarguard:training:job:{job_id}:cancel", "1", ex=3600)
            client.close()
        except Exception:
            pass
    else:
        return JSONResponse({"error": "Job is not cancellable"}, status_code=400)

    from starlette.responses import RedirectResponse

    return RedirectResponse(url="/admin/training/jobs", status_code=303)


# ── Detector state ───────────────────────────────────────────────────────────


@router.get("/detector-state")
async def detector_state(request: Request) -> Response:
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate
    return JSONResponse(_get_detector_state())


def _parse_result(raw: str | None) -> dict | None:
    """Parse a job result JSON string into a dict for template rendering."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _effective_result(result: dict | None) -> dict:
    if not isinstance(result, dict):
        return {}
    nested = result.get("train")
    return nested if isinstance(nested, dict) else result


def _result_view(result: dict | None, raw_execution: str | None = None) -> dict:
    """Flatten prepare-and-train results into a stable UI representation."""
    effective = _effective_result(result)
    try:
        persisted = json.loads(raw_execution or "{}")
    except (json.JSONDecodeError, TypeError):
        persisted = {}
    if not isinstance(persisted, dict):
        persisted = {}
    final_execution = effective.get("execution")
    if not isinstance(final_execution, dict):
        final_execution = {}
    execution = {**persisted, **final_execution}
    # An outer exception can contribute only final_exception/traceback. Keep
    # the richer resource dictionaries already durably sampled in that case.
    for nested_key in ("preflight", "resources", "cgroup_event_delta", "versions"):
        old = persisted.get(nested_key)
        new = final_execution.get(nested_key)
        if isinstance(old, dict) and isinstance(new, dict):
            execution[nested_key] = {**old, **new}
    error = effective.get("error") or (result or {}).get("error")
    final_exception = execution.get("final_exception")
    if not final_exception and error:
        final_exception = str(error).splitlines()[-1]
    log_path = execution.get("log_path") or effective.get("log_path")
    resources = execution.get("resources", {})
    preflight = execution.get("preflight", {})
    return {
        "error": error,
        "final_exception": final_exception,
        "traceback": execution.get("final_traceback"),
        "model_path": effective.get("model_path"),
        "checkpoint_path": effective.get("checkpoint_path") or execution.get("checkpoint_path"),
        "resume_from": effective.get("resume_from"),
        "log_path": log_path,
        "signal": execution.get("signal"),
        "return_code": execution.get("return_code"),
        "probable_oom": bool(execution.get("probable_oom", False)),
        "oom_evidence": execution.get("oom_evidence", []),
        "cuda_allocation_failure": bool(execution.get("cuda_allocation_failure", False)),
        "diagnostic_masking": execution.get("diagnostic_masking"),
        "memory_peak_bytes": execution.get("memory_peak_bytes"),
        "cgroup_event_delta": execution.get("cgroup_event_delta", {}),
        "preflight": preflight,
        "resources": resources,
    }


def _resume_candidate(job: dict) -> Path | None:
    if job.get("status") != "failed" or job.get("type") not in {"train", "prepare_and_train"}:
        return None
    result = _parse_result(job.get("result"))
    effective = _effective_result(result)
    raw_execution = effective.get("execution")
    execution: dict = raw_execution if isinstance(raw_execution, dict) else {}
    advertised = (
        effective.get("checkpoint_path")
        or execution.get("checkpoint_path")
        or effective.get("resume_from")
        or execution.get("resume_from")
    )
    if advertised:
        try:
            return validate_resume_checkpoint(advertised, RUNS_DIR)
        except (ValueError, OSError):
            pass

    try:
        started = datetime.fromisoformat(str(job.get("started_at"))).timestamp()
    except (TypeError, ValueError):
        return None
    try:
        completed = datetime.fromisoformat(str(job.get("completed_at"))).timestamp()
    except (TypeError, ValueError):
        completed = float("inf")
    candidates: list[Path] = []
    if not RUNS_DIR.is_dir():
        return None
    for path in RUNS_DIR.glob("*/weights/last.pt"):
        try:
            valid = validate_resume_checkpoint(path, RUNS_DIR)
            modified = valid.stat().st_mtime
            if started - 5 <= modified <= completed + 5:
                candidates.append(valid)
        except (ValueError, OSError):
            continue
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _newest_failed_resume_candidate() -> tuple[str, Path] | None:
    candidates: list[tuple[str, Path]] = []
    for row in db_module.get_training_jobs(limit=100, status="failed"):
        job = dict(row)
        checkpoint = _resume_candidate(job)
        if checkpoint:
            candidates.append((str(job["id"]), checkpoint))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[1].stat().st_mtime)


def _get_detector_state() -> dict:
    try:
        client = make_sync_client(_redis_cfg())
        raw: str | None = client.get(STATE_KEY)  # type: ignore[assignment,unused-ignore]
        client.close()
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {"state": "unknown"}
