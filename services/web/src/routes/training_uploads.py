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
from route_auth import current_role, require_admin, require_viewer
from starlette.responses import Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/training/uploads")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

TRAINING_UPLOADS_DIR = Path(os.environ.get("TRAINING_UPLOADS_DIR", "/data/training_uploads"))
MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/models"))
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".avi", ".mkv", ".mov"}
MODEL_EXTENSIONS = {".pt", ".engine", ".onnx"}
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
_HINT_RE = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,31}$")
_MAX_HINTS = 8
_RELABEL_CLASS_RE = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,63}$")
_MAX_RELABEL_BOXES = 32


def _validate_corrected_bboxes(raw: str) -> str | None:
    """Validate a JSON payload of human-drawn replacement boxes.

    Expected shape: ``[{"cls": "duck", "bbox": [xc, yc, w, h]}, ...]`` with
    bbox values normalized to 0-1. Returns the canonical JSON string to
    store, or ``None`` if the payload is empty / invalid.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data or len(data) > _MAX_RELABEL_BOXES:
        return None
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            return None
        cls = item.get("cls")
        bbox = item.get("bbox")
        if not isinstance(cls, str) or not _RELABEL_CLASS_RE.match(cls.lower().strip()):
            return None
        if not isinstance(bbox, list) or len(bbox) != 4:
            return None
        try:
            xc, yc, w, h = (float(v) for v in bbox)
        except (TypeError, ValueError):
            return None
        if not (0.0 < w <= 1.0 and 0.0 < h <= 1.0):
            return None
        # Reject boxes whose extents fall outside the image — clamping the
        # center alone allows e.g. xc=1.0, w=0.6 (right edge at 1.3).
        if not (0.0 <= xc - w / 2 and xc + w / 2 <= 1.0):
            return None
        if not (0.0 <= yc - h / 2 and yc + h / 2 <= 1.0):
            return None
        out.append({
            "cls": cls.lower().strip(),
            "bbox": [round(xc, 6), round(yc, 6), round(w, 6), round(h, 6)],
        })
    return json.dumps(out)


def _list_model_filenames() -> list[str]:
    """Names (not paths) of model files in MODELS_DIR. Empty list on error."""
    try:
        return sorted(
            f.name for f in MODELS_DIR.iterdir()
            if f.is_file() and f.suffix in MODEL_EXTENSIONS
        )
    except OSError:
        return []


def _parse_hints_input(raw: str) -> tuple[list[str], str | None]:
    """Parse a comma-separated hint string into (validated list, error).

    Returns ([], error) on validation failure, (items, None) on success.
    """
    items: list[str] = []
    for part in raw.split(","):
        s = part.strip().lower()
        if not s:
            continue
        if not _HINT_RE.match(s):
            return [], f"Invalid hint '{s}' — use letters, digits, spaces, _ or -"
        if s not in items:
            items.append(s)
    if len(items) > _MAX_HINTS:
        return [], f"Too many hints (max {_MAX_HINTS})"
    return items, None


def _validate_model_filename(name: str) -> str | None:
    """Resolve a user-supplied model name to a filename that exists in MODELS_DIR.

    Whitelist-lookup: enumerate known files and match by name, so the returned
    string originates from the filesystem listing, never user input.
    """
    name = name.strip()
    if not name:
        return None
    for known in _list_model_filenames():
        if known == name:
            return known
    return None


def _decode_hints(raw: str | None) -> list[str]:
    """Best-effort JSON decode of the hints column. Empty list on any failure."""
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(x) for x in items] if isinstance(items, list) else []


def _enrich_upload(row: dict) -> dict:
    """Add ``hints_list`` to an upload dict for template rendering."""
    row["hints_list"] = _decode_hints(row.get("hints"))
    return row


def _validate_confidence(raw: str) -> tuple[float | None, str | None]:
    """Parse confidence input. Empty → (None, None); range-check otherwise."""
    raw = raw.strip()
    if not raw:
        return None, None
    try:
        val = float(raw)
    except ValueError:
        return None, "Confidence threshold must be a number"
    if not (0.0 <= val <= 1.0):
        return None, "Confidence threshold must be between 0.0 and 1.0"
    return val, None


def _safe_upload_dir(upload_id: str) -> Path | None:
    """Resolve an upload_id to a directory path, or None if invalid.

    Uses a whitelist-lookup pattern (enumerate known directories, match
    against user input) so the returned Path originates from the filesystem
    listing, not from the user-controlled route parameter.  This breaks
    the taint chain that CodeQL traces from route param → Path constructor.
    """
    if not _SAFE_ID.match(upload_id):
        return None
    if db_module.get_training_upload(upload_id) is None:
        return None
    try:
        for entry in TRAINING_UPLOADS_DIR.iterdir():
            if entry.is_dir() and entry.name == upload_id:
                return entry
    except FileNotFoundError:
        pass
    return None


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
            "uploads": [_enrich_upload(dict(u)) for u in db_module.get_training_uploads(limit=PAGE_SIZE)],
            "total": db_module.count_training_uploads(),
            "page": 1,
            "total_pages": 1,
            "filter_status": "",
            "error": error,
            "available_models": _list_model_filenames(),
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
            "uploads": [_enrich_upload(dict(u)) for u in uploads],
            "total": total,
            "page": page,
            "total_pages": max(1, -(-total // PAGE_SIZE)),
            "filter_status": status,
            "error": None,
            "available_models": _list_model_filenames(),
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
    detector_model: str = Form(""),
    confidence_threshold: str = Form(""),
    hints: str = Form(""),
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

    # Legacy single-hint field still works on its own; the multi-value
    # `hints` form field is preferred. We keep both in sync below.
    parsed_hints, hint_err = _parse_hints_input(hints)
    if hint_err:
        return _error_response(request, hint_err)
    legacy_hint = target_class_hint.strip().lower() or None
    if legacy_hint and not _HINT_RE.match(legacy_hint):
        return _error_response(request, f"Invalid target class hint: {legacy_hint}")
    if legacy_hint and legacy_hint not in parsed_hints:
        parsed_hints.insert(0, legacy_hint)
    if not legacy_hint and parsed_hints:
        legacy_hint = parsed_hints[0]

    model_name = _validate_model_filename(detector_model) if detector_model.strip() else None
    if detector_model.strip() and model_name is None:
        return _error_response(request, f"Unknown model file: {detector_model.strip()}")

    conf, conf_err = _validate_confidence(confidence_threshold)
    if conf_err:
        return _error_response(request, conf_err)

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

        db_module.create_training_upload(
            upload_id, filename, legacy_hint,
            detector_model=model_name,
            confidence_threshold=conf,
            hints=json.dumps(parsed_hints) if parsed_hints else None,
        )
        logger.info("Training video uploaded: %s (%s, %.0fs)", filename, upload_id, duration)
        return RedirectResponse(url="/admin/training/uploads", status_code=303)

    finally:
        if tmp_path.exists() and tmp_path != (TRAINING_UPLOADS_DIR / upload_id / f"original{suffix}"):
            tmp_path.unlink(missing_ok=True)


# ── Edit upload settings (inline form on the list page) ─────────────────────


@router.post("/{upload_id}/settings")
async def update_upload_settings(
    request: Request,
    upload_id: str,
    target_class_hint: str = Form(""),
    detector_model: str = Form(""),
    confidence_threshold: str = Form(""),
    hints: str = Form(""),
) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    if not _SAFE_ID.match(upload_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    if db_module.get_training_upload(upload_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    parsed_hints, hint_err = _parse_hints_input(hints)
    if hint_err:
        return _error_response(request, hint_err)
    legacy_hint = target_class_hint.strip().lower() or None
    if legacy_hint and not _HINT_RE.match(legacy_hint):
        return _error_response(request, f"Invalid target class hint: {legacy_hint}")
    if legacy_hint and legacy_hint not in parsed_hints:
        parsed_hints.insert(0, legacy_hint)
    if not legacy_hint and parsed_hints:
        legacy_hint = parsed_hints[0]

    model_name = _validate_model_filename(detector_model) if detector_model.strip() else None
    if detector_model.strip() and model_name is None:
        return _error_response(request, f"Unknown model file: {detector_model.strip()}")

    conf, conf_err = _validate_confidence(confidence_threshold)
    if conf_err:
        return _error_response(request, conf_err)

    db_module.update_training_upload_settings(
        upload_id,
        target_class_hint=legacy_hint,
        detector_model=model_name,
        confidence_threshold=conf,
        hints=json.dumps(parsed_hints) if parsed_hints else None,
    )
    logger.info("Updated settings for training upload %s", upload_id)
    return RedirectResponse(url=_UPLOAD_LIST_URL, status_code=303)


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
    corrected_bboxes: str = Form(""),
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
    bboxes_json = _validate_corrected_bboxes(corrected_bboxes) if action == "corrected" else None
    if action == "corrected" and not corrected and not bboxes_json:
        return JSONResponse(
            {"error": "corrected_class or corrected_bboxes required"},
            status_code=400,
        )
    if action == "corrected" and corrected_bboxes and bboxes_json is None:
        return JSONResponse({"error": "invalid corrected_bboxes payload"}, status_code=400)

    db_module.update_training_event_review(event_id, action, corrected, bboxes_json)

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
            # Partial render bypasses base.html's _is_admin derivation, so
            # pass it explicitly or the action buttons vanish after HTMX swap.
            "_is_admin": current_role(request) == "admin",
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

    upload_dir = _safe_upload_dir(upload_id)
    if upload_dir is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    verified_id = upload_dir.name
    count = db_module.bulk_update_training_events(
        verified_id,
        action,
        filter_pass=filter_pass or None,
        filter_review_state="pending",
    )
    logger.info("Bulk %s %d events in upload %s", action, count, verified_id)
    return RedirectResponse(
        url=f"{_UPLOAD_LIST_URL}/{verified_id}",
        status_code=303,
    )


# ── Frame browser ────────────────────────────────────────────────────────────


_BROWSE_DEFAULT_STRIDE = 30
_BROWSE_PAGE_SIZE = 60
_BROWSE_MAX_STRIDE = 600


def _list_frame_indices(upload_dir: Path) -> list[int]:
    """Enumerate available frame indices on disk for an upload."""
    frames_dir = upload_dir / "frames"
    if not frames_dir.is_dir():
        return []
    out: list[int] = []
    try:
        for entry in frames_dir.iterdir():
            if entry.suffix.lower() != ".jpg":
                continue
            stem = entry.stem
            if stem.isdigit():
                out.append(int(stem))
    except OSError:
        return []
    out.sort()
    return out


@router.get("/{upload_id}/browse", response_class=HTMLResponse)
async def browse_grid(
    request: Request,
    upload_id: str,
    page: int = 1,
    stride: str = "",
) -> Response:
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate

    upload_dir = _safe_upload_dir(upload_id)
    if upload_dir is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    verified_id = upload_dir.name
    upload = db_module.get_training_upload(verified_id)
    if upload is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    all_frames = _list_frame_indices(upload_dir)
    if stride == "all":
        sampled = all_frames
        stride_value = 1
    else:
        try:
            stride_value = int(stride) if stride else _BROWSE_DEFAULT_STRIDE
        except ValueError:
            stride_value = _BROWSE_DEFAULT_STRIDE
        stride_value = max(1, min(_BROWSE_MAX_STRIDE, stride_value))
        sampled = [f for f in all_frames if f % stride_value == 0]

    if page < 1:
        page = 1
    total = len(sampled)
    total_pages = max(1, (total + _BROWSE_PAGE_SIZE - 1) // _BROWSE_PAGE_SIZE)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * _BROWSE_PAGE_SIZE
    page_frames = sampled[start : start + _BROWSE_PAGE_SIZE]

    events_by_frame = db_module.get_training_events_by_frames(verified_id, page_frames)

    tiles = []
    for frame_idx in page_frames:
        events = events_by_frame.get(frame_idx, [])
        has_detection = any(ev["detection_pass"] != "manual" for ev in events)
        has_manual = any(ev["detection_pass"] == "manual" for ev in events)
        has_annotation = any(
            ev["corrected_bboxes"] for ev in events if ev["review_state"] == "corrected"
        )
        tiles.append({
            "frame_idx": frame_idx,
            "has_detection": has_detection,
            "has_manual": has_manual,
            "has_annotation": has_annotation,
        })

    return templates.TemplateResponse(
        request,
        "training_browse.html",
        {
            "upload": dict(upload),
            "tiles": tiles,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "all_frames_total": len(all_frames),
            "stride": stride_value,
            "stride_param": "all" if stride == "all" else str(stride_value),
            "showing_all": stride == "all",
        },
    )


@router.get("/{upload_id}/browse/{frame_idx}", response_class=HTMLResponse)
async def browse_frame(
    request: Request,
    upload_id: str,
    frame_idx: int,
) -> Response:
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate

    upload_dir = _safe_upload_dir(upload_id)
    if upload_dir is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    verified_id = upload_dir.name
    upload = db_module.get_training_upload(verified_id)
    if upload is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if frame_idx < 0:
        return JSONResponse({"error": "invalid frame index"}, status_code=400)

    frame_path = upload_dir / "frames" / f"{frame_idx:06d}.jpg"
    if not frame_path.exists():
        return JSONResponse({"error": "frame not found"}, status_code=404)

    # If a detector event already exists for this frame, send the user to the
    # existing labeling queue so re-label augments that event instead of
    # creating a duplicate manual row.
    existing = db_module.get_training_events_by_frames(verified_id, [frame_idx]).get(frame_idx, [])
    detector_event = next(
        (ev for ev in existing if ev["detection_pass"] != "manual"),
        None,
    )
    if detector_event is not None:
        return RedirectResponse(
            url=f"{_UPLOAD_LIST_URL}/{verified_id}?event_id={detector_event['id']}",
            status_code=303,
        )

    manual_event = next(
        (ev for ev in existing if ev["detection_pass"] == "manual"),
        None,
    )

    # Stride + page context so prev/next nav uses the same sampling the user
    # entered with.
    stride_q = request.query_params.get("stride", "")
    all_frames = _list_frame_indices(upload_dir)
    if stride_q == "all":
        sampled = all_frames
    else:
        try:
            stride_value = int(stride_q) if stride_q else _BROWSE_DEFAULT_STRIDE
        except ValueError:
            stride_value = _BROWSE_DEFAULT_STRIDE
        stride_value = max(1, min(_BROWSE_MAX_STRIDE, stride_value))
        sampled = [f for f in all_frames if f % stride_value == 0]

    prev_frame = next_frame = None
    if frame_idx in sampled:
        i = sampled.index(frame_idx)
        if i > 0:
            prev_frame = sampled[i - 1]
        if i < len(sampled) - 1:
            next_frame = sampled[i + 1]

    import config_store
    cfg = config_store.load_cached()
    target_classes = cfg.get("detection", {}).get("target_classes", [])

    return templates.TemplateResponse(
        request,
        "training_browse_frame.html",
        {
            "upload": dict(upload),
            "frame_idx": frame_idx,
            "prev_frame": prev_frame,
            "next_frame": next_frame,
            "stride_param": stride_q or "",
            "manual_event": dict(manual_event) if manual_event else None,
            "target_classes": target_classes,
        },
    )


@router.post("/{upload_id}/browse/{frame_idx}/annotate")
async def annotate_frame(
    request: Request,
    upload_id: str,
    frame_idx: int,
    corrected_bboxes: str = Form(...),
) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate

    upload_dir = _safe_upload_dir(upload_id)
    if upload_dir is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    verified_id = upload_dir.name
    if frame_idx < 0:
        return JSONResponse({"error": "invalid frame index"}, status_code=400)
    frame_path = upload_dir / "frames" / f"{frame_idx:06d}.jpg"
    if not frame_path.exists():
        return JSONResponse({"error": "frame not found"}, status_code=404)

    bboxes_json = _validate_corrected_bboxes(corrected_bboxes)
    if bboxes_json is None:
        return JSONResponse({"error": "invalid corrected_bboxes payload"}, status_code=400)

    # Detector event already exists → redirect to label queue, do not duplicate.
    existing = db_module.get_training_events_by_frames(verified_id, [frame_idx]).get(frame_idx, [])
    detector_event = next(
        (ev for ev in existing if ev["detection_pass"] != "manual"),
        None,
    )
    if detector_event is not None:
        return JSONResponse(
            {"error": "frame already has a detector event — re-label it from the queue"},
            status_code=409,
        )

    manual_event = next(
        (ev for ev in existing if ev["detection_pass"] == "manual"),
        None,
    )
    if manual_event is not None:
        db_module.update_training_event_review(
            int(manual_event["id"]), "corrected", None, bboxes_json,
        )
        logger.info("Updated manual training_event %s for upload %s frame %s",
                    manual_event["id"], verified_id, frame_idx)
    else:
        new_id = db_module.insert_manual_training_event(verified_id, frame_idx, bboxes_json)
        logger.info("Inserted manual training_event %s for upload %s frame %s",
                    new_id, verified_id, frame_idx)

    return JSONResponse({"ok": True})
