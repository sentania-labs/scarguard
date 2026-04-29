"""Training data dashboard, YOLO-format dataset export, and model evaluation."""

from __future__ import annotations

import asyncio
import html as _html
import io
import json
import logging
import os
import zipfile
from datetime import datetime
from datetime import timezone as _tz
from pathlib import Path

import config_store
import db
import redis.asyncio as aioredis
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from route_auth import require_admin, require_viewer
from starlette.responses import Response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/training")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/data/snapshots")
MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/models"))
ALLOWED_MODEL_EXTENSIONS = {".pt", ".engine", ".onnx"}

EVAL_REQUEST_CHANNEL = "scarguard:eval:request"
EVAL_PROGRESS_KEY = "scarguard:eval:progress"
EVAL_RESULT_KEY = "scarguard:eval:result"


@router.get("", response_class=HTMLResponse)
async def training_dashboard(
    request: Request,
    date_from: str = "",
    date_to: str = "",
) -> Response:
    """Training data quality dashboard. Readable by viewer and admin."""
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate
    dfrom = date_from or None
    dto = date_to or None
    stats = db.get_feedback_stats(date_from=dfrom, date_to=dto)

    # Build per-class data for the bar chart.  Training-positive count =
    # correct + wrong_class (a wrong_class correction relabels the bbox to
    # this effective class, so it's a training sample for it).  False
    # positives are tracked separately — they become background samples in
    # the export, not labeled instances.
    by_class = stats["by_class"]
    max_positive = max(
        (v["correct"] + v["wrong_class"] for v in by_class.values()),
        default=1,
    ) or 1

    class_chart: list[dict] = []
    for cls_name, counts in sorted(by_class.items()):
        positive = counts["correct"] + counts["wrong_class"]
        class_chart.append({
            "name": cls_name.replace("_", " ").title(),
            "raw_name": cls_name,
            "correct": counts["correct"],
            "false_positive": counts["false_positive"],
            "wrong_class": counts["wrong_class"],
            "positive": positive,
            "bar_pct": (positive / max_positive) * 100,
            "low_data": positive < 500,
        })

    # Count exportable events (correct + wrong_class with bbox + false_positive
    # as background samples)
    exportable = db.count_exportable_events(date_from=dfrom, date_to=dto)

    return templates.TemplateResponse(
        request,
        "training.html",
        {
            "stats": stats,
            "class_chart": class_chart,
            "exportable": exportable,
            "filter_date_from": date_from,
            "filter_date_to": date_to,
        },
    )


@router.get("/export")
async def export_dataset(
    request: Request,
    date_from: str = "",
    date_to: str = "",
) -> Response:
    """Generate a YOLO-format dataset zip from confirmed detections.

    Admin only — exporting labeled data is treated as a write-equivalent
    action (data leaves the system) so viewers are blocked.
    """
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    dfrom = date_from or None
    dto = date_to or None
    rows = db.get_exportable_events(date_from=dfrom, date_to=dto)

    if not rows:
        return StreamingResponse(
            iter([b"No exportable events found."]),
            media_type="text/plain",
            status_code=404,
        )

    # Build class-to-index mapping from distinct labels.  Skip false
    # positives — they become background samples (image with empty label
    # file) and don't contribute a class to data.yaml.
    class_set: set[str] = set()
    for r in rows:
        if r["feedback"] == "false_positive":
            continue
        class_set.add(_effective_class(r))
    class_names = sorted(class_set)
    class_to_idx = {name: idx for idx, name in enumerate(class_names)}

    # Stream a zip file
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Write data.yaml
        data_yaml = _build_data_yaml(class_names)
        zf.writestr("dataset/data.yaml", data_yaml)

        for r in rows:
            row = dict(r)
            event_id = row["id"]
            snapshot_path = row["snapshot_path"]
            is_negative = row["feedback"] == "false_positive"

            # Copy snapshot image (always — positives and negatives both
            # need the pixels; only the label file differs)
            src_path = Path(snapshot_path)
            if not src_path.exists():
                # Try in SNAPSHOT_DIR
                src_path = Path(SNAPSHOT_DIR) / src_path.name
            if not src_path.exists():
                logger.warning("Snapshot missing for event %d: %s", event_id, snapshot_path)
                continue

            img_name = f"{event_id}.jpg"
            zf.write(str(src_path), f"dataset/images/train/{img_name}")

            if is_negative:
                # Background sample: empty label file is YOLO's canonical
                # "no targets in this image" signal.
                zf.writestr(f"dataset/labels/train/{event_id}.txt", "")
                continue

            # Positive (correct / wrong_class): emit YOLO bbox annotation
            bbox = json.loads(row["bbox"]) if isinstance(row["bbox"], str) else row["bbox"]
            frame_size = json.loads(row["frame_size"]) if isinstance(row["frame_size"], str) else row["frame_size"]
            class_idx = class_to_idx[_effective_class(row)]
            x1, y1, x2, y2 = bbox
            fw, fh = frame_size
            x_center = ((x1 + x2) / 2) / fw
            y_center = ((y1 + y2) / 2) / fh
            width = (x2 - x1) / fw
            height = (y2 - y1) / fh
            annotation = f"{class_idx} {x_center:.6f} {y_center:.6f} {width:.6f} {height:.6f}\n"
            zf.writestr(f"dataset/labels/train/{event_id}.txt", annotation)

    # Record export date for training nudge
    db.set_app_state("last_export_date", datetime.now(_tz.utc).isoformat())

    buf.seek(0)
    return StreamingResponse(
        iter([buf.read()]),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=scarguard_dataset.zip"},
    )


def _effective_class(row: object) -> str:
    """Return the effective class name, using corrected_class for wrong_class feedback."""
    r: dict = dict(row) if not isinstance(row, dict) else row  # type: ignore[call-overload]
    if r.get("feedback") == "wrong_class" and r.get("corrected_class"):
        return r["corrected_class"]
    return r["class_name"]


def _build_data_yaml(class_names: list[str]) -> str:
    """Generate a YOLO data.yaml file content."""
    lines = [
        "# ScarGuard exported dataset",
        "path: .",
        "train: images/train",
        "val: images/train  # split manually if desired",
        "",
        f"nc: {len(class_names)}",
        f"names: {class_names}",
        "",
    ]
    return "\n".join(lines)


def _list_models() -> list[dict]:
    """Return available model files from the models directory."""
    if not MODELS_DIR.exists():
        return []
    return sorted(
        [
            {"name": f.name, "size_mb": round(f.stat().st_size / 1_048_576, 1)}
            for f in MODELS_DIR.iterdir()
            if f.is_file() and f.suffix in ALLOWED_MODEL_EXTENSIONS
        ],
        key=lambda x: str(x["name"]),
    )


# ── Model Evaluation ──────────────────────────────────────────────────────


@router.get("/evaluate", response_class=HTMLResponse)
async def evaluate_page(request: Request) -> Response:
    """Model evaluation comparison page. Readable by viewer and admin."""
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate
    models = _list_models()
    cfg = config_store.load_cached()
    current_model = cfg.get("detection", {}).get("model_path", "")
    return templates.TemplateResponse(
        request,
        "evaluate.html",
        {
            "models": models,
            "current_model": Path(current_model).name if current_model else "",
        },
    )


@router.post("/evaluate", response_class=HTMLResponse)
async def start_evaluation(
    request: Request,
    model_a: str = Form(...),
    model_b: str = Form(...),
    date_from: str = Form(""),
    date_to: str = Form(""),
) -> Response:
    """Publish an evaluation request to Redis for the detector to process.

    Admin only — kicking off an evaluation runs GPU inference on the
    detector and is a write-equivalent action.
    """
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    cfg = config_store.load_cached()
    redis_cfg = cfg.get("redis", {})
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))

    # Resolve model paths
    model_a_path = str(MODELS_DIR / model_a) if model_a else ""
    model_b_path = str(MODELS_DIR / model_b) if model_b else ""

    eval_request = {
        "model_a": model_a_path,
        "model_b": model_b_path,
        "date_from": date_from or None,
        "date_to": date_to or None,
    }

    client = aioredis.Redis(host=host, port=port, password=os.environ.get("REDIS_PASSWORD", "") or None, decode_responses=True)
    try:
        await client.publish(EVAL_REQUEST_CHANNEL, json.dumps(eval_request))
    finally:
        await client.aclose()  # type: ignore[attr-defined,unused-ignore]

    return HTMLResponse(
        '<div class="alert alert-ok">Evaluation started. Results will appear below.</div>'
    )


@router.get("/evaluate/stream")
async def evaluate_stream(request: Request) -> Response:
    """SSE stream — polls Redis for evaluation progress and results.

    Read-only; viewers can watch an in-flight evaluation kicked off by an
    admin.
    """
    gate = require_viewer(request, is_api=True)
    if not isinstance(gate, dict):
        return gate
    cfg = config_store.load_cached()
    redis_cfg = cfg.get("redis", {})
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))

    max_poll_seconds = 600  # 10-minute timeout

    async def generator():
        client = aioredis.Redis(host=host, port=port, password=os.environ.get("REDIS_PASSWORD", "") or None, decode_responses=True)
        try:
            yield ": connected\n\n"
            elapsed = 0.0
            while elapsed < max_poll_seconds:
                if await request.is_disconnected():
                    break

                # Check progress
                progress_raw = await client.get(EVAL_PROGRESS_KEY)
                if progress_raw:
                    progress = json.loads(progress_raw)
                    yield f"event: progress\ndata: {json.dumps(progress)}\n\n"

                    if progress.get("status") in ("complete", "error"):
                        # Fetch final result
                        result_raw = await client.get(EVAL_RESULT_KEY)
                        if result_raw:
                            yield f"event: result\ndata: {result_raw}\n\n"
                        break

                await asyncio.sleep(1)
                elapsed += 1.0
            else:
                # Timeout reached
                timeout_msg = json.dumps({"status": "error", "error": "Evaluation timed out"})
                yield f"event: result\ndata: {timeout_msg}\n\n"
        finally:
            await client.aclose()

    return StreamingResponse(generator(), media_type="text/event-stream")


@router.post("/promote", response_class=HTMLResponse)
async def promote_model(
    request: Request,
    model_path: str = Form(...),
) -> Response:
    """Update the active model in scarguard.yml, triggering hot-reload.

    Admin only — changes the active detection model for all cameras.
    """
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    safe_name = Path(model_path).name  # strip directory components
    model_file = MODELS_DIR / safe_name

    # Validate resolved path stays inside MODELS_DIR
    if not model_file.resolve().is_relative_to(MODELS_DIR.resolve()):
        return HTMLResponse(
            '<div class="alert alert-err">Invalid model path.</div>',
            status_code=400,
        )

    if not model_file.exists():
        return HTMLResponse(
            f'<div class="alert alert-err">Model not found: {_html.escape(safe_name)}</div>',
            status_code=404,
        )

    # Update config
    cfg = config_store.load()
    cfg.setdefault("detection", {})["model_path"] = f"/models/{safe_name}"
    config_store.save(cfg)

    return HTMLResponse(
        f'<div class="alert alert-ok">Model promoted: {_html.escape(safe_name)}. '
        f"Hot-reload will apply the change.</div>"
    )
