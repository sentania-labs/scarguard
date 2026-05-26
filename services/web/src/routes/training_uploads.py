"""Training video upload, labeling queue, and review endpoints."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import db as db_module
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from rate_limit_dep import rate_limit
from route_auth import require_admin, require_viewer
from starlette.responses import Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/training/uploads")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

TRAINING_UPLOADS_DIR = Path(os.environ.get("TRAINING_UPLOADS_DIR", "/data/training_uploads"))
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".avi", ".mkv", ".mov"}
MAX_DURATION_SECONDS = 60
_DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
UPLOAD_CHUNK_SIZE = int(os.environ.get("TRAINING_UPLOAD_CHUNK_SIZE", str(_DEFAULT_CHUNK_SIZE)))
MAX_UPLOAD_BYTES: int | None = (
    int(os.environ["TRAINING_UPLOAD_MAX_BYTES"])
    if "TRAINING_UPLOAD_MAX_BYTES" in os.environ
    else 500 * 1024 * 1024  # 500 MB default
)
PAGE_SIZE = 25
_UPLOAD_LIST_URL = "/admin/training/uploads"

_SAFE_ID = re.compile(r"^[0-9a-f]{32}$")


def _safe_upload_dir(upload_id: str) -> Path | None:
    """Resolve an upload_id to a directory path, or None if invalid.

    Validates the ID is a hex-only string AND exists in the database.
    Returns the constructed path using the DB-returned ID (not user input).
    """
    if not _SAFE_ID.match(upload_id):
        return None
    upload = db_module.get_training_upload(upload_id)
    if upload is None:
        return None
    safe_id: str = upload["id"]
    return TRAINING_UPLOADS_DIR / safe_id


def _probe_duration(file_path: Path) -> float | None:
    """Use ffprobe to get video duration in seconds. Returns None on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception:
        logger.exception("ffprobe failed for %s", file_path)
        return None


def _error_response(request: Request, error: str, status_code: int = 400) -> Response:
    return templates.TemplateResponse(
        request,
        "training_uploads.html",
        {
            "uploads": db_module.get_training_uploads(limit=PAGE_SIZE),
            "total": db_module.count_training_uploads(),
            "page": 1,
            "total_pages": 1,
            "filter_status": "",
            "error": error,
        },
        status_code=status_code,
    )


# ── Upload list page ─────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def uploads_list_page(
    request: Request,
    page: int = 1,
    status: str = "",
) -> Response:
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate
    st = status or None
    offset = (page - 1) * PAGE_SIZE
    uploads = db_module.get_training_uploads(limit=PAGE_SIZE, offset=offset, status=st)
    total = db_module.count_training_uploads(status=st)
    return templates.TemplateResponse(
        request,
        "training_uploads.html",
        {
            "uploads": [dict(u) for u in uploads],
            "total": total,
            "page": page,
            "total_pages": max(1, -(-total // PAGE_SIZE)),
            "filter_status": status,
            "error": None,
        },
    )


# ── Upload video ─────────────────────────────────────────────────────────────


@router.post(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit("training-upload", capacity=10, window_seconds=3600))],
)
async def upload_video(
    request: Request,
    file: UploadFile = File(...),
    target_class_hint: str = Form(""),
) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate

    filename = file.filename or "upload"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_VIDEO_EXTENSIONS:
        return _error_response(
            request,
            f"Unsupported format '{suffix}'. Accepted: {', '.join(sorted(ALLOWED_VIDEO_EXTENSIONS))}",
        )

    hint = target_class_hint.strip() or None
    if hint and hint not in ("duck", "heron", "raccoon", "background"):
        return _error_response(request, f"Invalid target class hint: {hint}")

    TRAINING_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex

    fd, tmp_path_str = tempfile.mkstemp(dir=str(TRAINING_UPLOADS_DIR), suffix=suffix)
    tmp_path = Path(tmp_path_str)
    try:
        bytes_written = 0
        with os.fdopen(fd, "wb") as f:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if MAX_UPLOAD_BYTES is not None and bytes_written > MAX_UPLOAD_BYTES:
                    return _error_response(
                        request,
                        f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
                    )
                f.write(chunk)

        duration = _probe_duration(tmp_path)
        if duration is None:
            return _error_response(request, "Could not read video — file may be corrupt or unsupported codec")
        if duration > MAX_DURATION_SECONDS:
            return _error_response(
                request,
                f"Video is {duration:.0f}s — max allowed is {MAX_DURATION_SECONDS}s",
            )

        upload_dir = TRAINING_UPLOADS_DIR / upload_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        final_path = upload_dir / f"original{suffix}"
        os.replace(str(tmp_path), str(final_path))
        tmp_path = final_path  # prevent cleanup of moved file

        db_module.create_training_upload(upload_id, filename, hint)
        logger.info("Training video uploaded: %s (%s, %.0fs)", filename, upload_id, duration)
        return RedirectResponse(url="/admin/training/uploads", status_code=303)

    finally:
        if tmp_path.exists() and tmp_path != (TRAINING_UPLOADS_DIR / upload_id / f"original{suffix}"):
            tmp_path.unlink(missing_ok=True)


# ── Delete upload ────────────────────────────────────────────────────────────


@router.post("/{upload_id}/delete")
async def delete_upload(request: Request, upload_id: str) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate

    upload_dir = _safe_upload_dir(upload_id)
    if upload_dir is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    db_module.delete_training_upload(upload_id)
    shutil.rmtree(upload_dir, ignore_errors=True)
    logger.info("Training upload deleted: %s", upload_id)
    return RedirectResponse(url=_UPLOAD_LIST_URL, status_code=303)


# ── Serve frame ──────────────────────────────────────────────────────────────


@router.get("/{upload_id}/frames/{frame_idx}")
async def serve_frame(request: Request, upload_id: str, frame_idx: int) -> Response:
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate

    upload_dir = _safe_upload_dir(upload_id)
    if upload_dir is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    if frame_idx < 0:
        return JSONResponse({"error": "invalid frame index"}, status_code=400)
    frame_path = upload_dir / "frames" / f"{frame_idx:06d}.jpg"
    if not frame_path.exists():
        return JSONResponse({"error": "frame not found"}, status_code=404)

    return FileResponse(str(frame_path), media_type="image/jpeg")


# ── Labeling queue page ─────────────────────────────────────────────────────


@router.get("/{upload_id}", response_class=HTMLResponse)
async def label_page(
    request: Request,
    upload_id: str,
    event_id: int = 0,
    review_state: str = "",
    detection_pass: str = "",
) -> Response:
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate

    if not _SAFE_ID.match(upload_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    upload = db_module.get_training_upload(upload_id)
    if not upload:
        return JSONResponse({"error": "not found"}, status_code=404)

    rs = review_state or None
    dp = detection_pass or None
    stats = db_module.get_training_upload_stats(upload_id)

    if event_id:
        event = db_module.get_training_event(event_id)
    else:
        events = db_module.get_training_events(upload_id, limit=1, review_state=rs, detection_pass=dp)
        event = events[0] if events else None

    next_event = None
    prev_event = None
    if event:
        next_event = db_module.get_next_training_event(
            upload_id, event["id"], review_state=rs, detection_pass=dp,
        )
        prev_event = db_module.get_prev_training_event(
            upload_id, event["id"], review_state=rs, detection_pass=dp,
        )

    import config_store
    cfg = config_store.load_cached()
    target_classes = cfg.get("detection", {}).get("target_classes", [])

    return templates.TemplateResponse(
        request,
        "training_label.html",
        {
            "upload": dict(upload),
            "event": dict(event) if event else None,
            "next_event": dict(next_event) if next_event else None,
            "prev_event": dict(prev_event) if prev_event else None,
            "stats": stats,
            "target_classes": target_classes,
            "filter_review_state": review_state,
            "filter_detection_pass": detection_pass,
        },
    )


# ── Review single event ─────────────────────────────────────────────────────


@router.post("/{upload_id}/events/{event_id}/review")
async def review_event(
    request: Request,
    upload_id: str,
    event_id: int,
    action: str = Form(...),
    corrected_class: str = Form(""),
) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate

    if action not in ("approved", "rejected", "corrected"):
        return JSONResponse({"error": "invalid action"}, status_code=400)

    event = db_module.get_training_event(event_id)
    if not event or event["upload_id"] != upload_id:
        return JSONResponse({"error": "event not found in this upload"}, status_code=404)

    corrected = corrected_class.strip()[:64] if action == "corrected" else None
    if action == "corrected" and not corrected:
        return JSONResponse({"error": "corrected_class required"}, status_code=400)

    db_module.update_training_event_review(event_id, action, corrected)

    rs = request.query_params.get("review_state", "")
    dp = request.query_params.get("detection_pass", "")

    next_event = db_module.get_next_training_event(
        upload_id, event_id,
        review_state=rs or None,
        detection_pass=dp or None,
    )
    event = next_event or db_module.get_training_event(event_id)
    stats = db_module.get_training_upload_stats(upload_id)

    if next_event:
        prev_event = db_module.get_prev_training_event(
            upload_id, next_event["id"],
            review_state=rs or None,
            detection_pass=dp or None,
        )
    else:
        prev_event = None

    import config_store
    cfg = config_store.load_cached()
    target_classes = cfg.get("detection", {}).get("target_classes", [])

    return templates.TemplateResponse(
        request,
        "partials/training_label_card.html",
        {
            "upload": dict(db_module.get_training_upload(upload_id)),  # type: ignore[arg-type]
            "event": dict(event) if event else None,
            "next_event": None if not next_event else (
                db_module.get_next_training_event(
                    upload_id, next_event["id"],
                    review_state=rs or None,
                    detection_pass=dp or None,
                )
            ),
            "prev_event": prev_event,
            "stats": stats,
            "target_classes": target_classes,
            "filter_review_state": rs,
            "filter_detection_pass": dp,
            "auto_advance": True,
        },
    )


# ── Bulk review ──────────────────────────────────────────────────────────────


@router.post("/{upload_id}/bulk-review")
async def bulk_review(
    request: Request,
    upload_id: str,
    action: str = Form(...),
    filter_pass: str = Form(""),
) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate

    if action not in ("approved", "rejected"):
        return JSONResponse({"error": "bulk action must be approved or rejected"}, status_code=400)

    if not _SAFE_ID.match(upload_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    upload = db_module.get_training_upload(upload_id)
    if not upload:
        return JSONResponse({"error": "not found"}, status_code=404)

    safe_id: str = upload["id"]
    count = db_module.bulk_update_training_events(
        safe_id,
        action,
        filter_pass=filter_pass or None,
        filter_review_state="pending",
    )
    logger.info("Bulk %s %d events in upload %s", action, count, safe_id)
    return RedirectResponse(
        url=f"{_UPLOAD_LIST_URL}/{safe_id}",
        status_code=303,
    )
