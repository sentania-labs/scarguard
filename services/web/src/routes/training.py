"""Training data dashboard and YOLO-format dataset export."""

from __future__ import annotations

import io
import json
import logging
import os
import zipfile
from pathlib import Path

import db
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/training")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/data/snapshots")


@router.get("", response_class=HTMLResponse)
async def training_dashboard(
    request: Request,
    date_from: str = "",
    date_to: str = "",
) -> HTMLResponse:
    """Training data quality dashboard."""
    dfrom = date_from or None
    dto = date_to or None
    stats = db.get_feedback_stats(date_from=dfrom, date_to=dto)

    # Build per-class data for the bar chart
    by_class = stats["by_class"]
    max_correct = max(
        (v["correct"] for v in by_class.values()), default=1
    ) or 1

    class_chart: list[dict] = []
    for cls_name, counts in sorted(by_class.items()):
        class_chart.append({
            "name": cls_name.replace("_", " ").title(),
            "raw_name": cls_name,
            "correct": counts["correct"],
            "false_positive": counts["false_positive"],
            "wrong_class": counts["wrong_class"],
            "bar_pct": (counts["correct"] / max_correct) * 100,
            "low_data": counts["correct"] < 500,
        })

    # Count exportable events (correct or wrong_class with bbox)
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
) -> StreamingResponse:
    """Generate a YOLO-format dataset zip from confirmed detections."""
    dfrom = date_from or None
    dto = date_to or None
    rows = db.get_exportable_events(date_from=dfrom, date_to=dto)

    if not rows:
        return StreamingResponse(
            iter([b"No exportable events found."]),
            media_type="text/plain",
            status_code=404,
        )

    # Build class-to-index mapping from distinct labels
    class_set: set[str] = set()
    for r in rows:
        label = _effective_class(r)
        class_set.add(label)
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
            bbox = json.loads(row["bbox"]) if isinstance(row["bbox"], str) else row["bbox"]
            frame_size = json.loads(row["frame_size"]) if isinstance(row["frame_size"], str) else row["frame_size"]
            label = _effective_class(row)
            class_idx = class_to_idx[label]

            # Copy snapshot image
            src_path = Path(snapshot_path)
            if not src_path.exists():
                # Try in SNAPSHOT_DIR
                src_path = Path(SNAPSHOT_DIR) / src_path.name
            if not src_path.exists():
                logger.warning("Snapshot missing for event %d: %s", event_id, snapshot_path)
                continue

            img_name = f"{event_id}.jpg"
            zf.write(str(src_path), f"dataset/images/train/{img_name}")

            # Write YOLO annotation
            x1, y1, x2, y2 = bbox
            fw, fh = frame_size
            x_center = ((x1 + x2) / 2) / fw
            y_center = ((y1 + y2) / 2) / fh
            width = (x2 - x1) / fw
            height = (y2 - y1) / fh
            annotation = f"{class_idx} {x_center:.6f} {y_center:.6f} {width:.6f} {height:.6f}\n"
            zf.writestr(f"dataset/labels/train/{event_id}.txt", annotation)

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
