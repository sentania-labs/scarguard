"""Training job submission, monitoring, and SSE progress endpoints."""

from __future__ import annotations

import json
import logging
import uuid

import db as db_module
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from redis_client import make_sync_client
from route_auth import require_admin, require_viewer
from starlette.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/training/jobs")
templates = Jinja2Templates(directory=str(__import__("pathlib").Path(__file__).parent.parent / "templates"))

JOB_NOTIFY_CHANNEL = "scarguard:training:job:notify"
STATE_KEY = "scarguard:detector:state"
PAGE_SIZE = 25

_VALID_JOB_TYPES = {"process_video", "prepare_dataset", "train", "prepare_and_train"}


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
    for j in jobs:
        d = dict(j)
        d["parsed_result"] = _parse_result(d.get("result"))
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
            "is_admin": require_admin(request) if not isinstance(require_admin(request), dict) else True,
            "upload_options": upload_options,
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
        upload_ids = [uid for uid in upload_ids_raw if isinstance(uid, str) and _UPLOAD_ID_RE.match(uid)]
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

    return JSONResponse({
        "job": dict(job),
        "progress": progress,
        "log": log_lines,
    })


# ── SSE progress stream ─────────────────────────────────────────────────────


@router.get("/{job_id}/stream")
async def job_stream(request: Request, job_id: str) -> Response:
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate

    import asyncio

    async def _stream():
        try:
            client = make_sync_client(_redis_cfg())
        except Exception:
            yield "event: error\ndata: {\"error\": \"Redis unavailable\"}\n\n"
            return

        progress_key = f"scarguard:training:job:{job_id}:progress"
        log_key = f"scarguard:training:job:{job_id}:log"
        last_log_len = 0
        max_iterations = 43200  # 12 hours at 1s intervals

        try:
            for _ in range(max_iterations):
                raw = client.get(progress_key)
                if raw:
                    yield f"event: progress\ndata: {raw}\n\n"

                current_log_len = client.llen(log_key)
                if current_log_len > last_log_len:
                    new_lines = client.lrange(log_key, last_log_len, current_log_len - 1)
                    if new_lines:
                        for line in new_lines:
                            yield f"event: log\ndata: {json.dumps(line)}\n\n"
                    last_log_len = current_log_len

                job = db_module.get_training_job(job_id)
                if job and job["status"] in ("completed", "failed", "cancelled"):
                    result_data = json.loads(job["result"]) if job["result"] else {}
                    yield f"event: result\ndata: {json.dumps({'status': job['status'], **result_data})}\n\n"
                    break

                await asyncio.sleep(1)
        finally:
            try:
                client.close()
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
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


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
