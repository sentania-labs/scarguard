"""Token-based feedback from notification links (unauthenticated)."""

import os
from pathlib import Path

import db
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/feedback")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_VALID_FEEDBACK = ("correct", "false_positive", "wrong_class")
_SNAPSHOT_DIR = Path(os.environ.get("SNAPSHOT_DIR", "/data/snapshots"))


@router.get("/{token}/snapshot")
async def feedback_snapshot(token: str) -> FileResponse:
    """Serve the detection snapshot for a valid feedback token (no auth)."""
    row = db.get_event_by_token(token)
    if row is None:
        raise HTTPException(status_code=404)
    event = dict(row)
    if not event.get("snapshot_path"):
        raise HTTPException(status_code=404)
    fname = Path(event["snapshot_path"]).name
    snapshot = _SNAPSHOT_DIR / fname
    if not snapshot.is_file() or snapshot.resolve().parent != _SNAPSHOT_DIR.resolve():
        raise HTTPException(status_code=404)
    return FileResponse(snapshot)


@router.get("/{token}", response_class=HTMLResponse)
async def feedback_page(request: Request, token: str, v: str = "") -> HTMLResponse:
    """Show the feedback confirmation page for a detection event."""
    row = db.get_event_by_token(token)
    if row is None:
        return templates.TemplateResponse(
            request,
            "feedback.html",
            {"event": None, "token": token, "error": "expired", "preselect": ""},
        )
    event = dict(row)
    if event.get("feedback"):
        return templates.TemplateResponse(
            request,
            "feedback.html",
            {"event": event, "token": token, "error": "already_used", "preselect": ""},
        )
    preselect = v if v in _VALID_FEEDBACK else ""
    return templates.TemplateResponse(
        request,
        "feedback.html",
        {"event": event, "token": token, "error": "", "preselect": preselect},
    )


@router.post("/{token}", response_class=HTMLResponse)
async def submit_feedback(
    request: Request,
    token: str,
    v: str = "",
    feedback: str = Form(""),
    corrected_class: str = Form(""),
) -> HTMLResponse:
    """Record feedback for a detection event via one-time token.

    Accepts feedback from either form data (web UI) or query param ``?v=``
    (ntfy action buttons POST with empty body).
    """
    row = db.get_event_by_token(token)
    if row is None:
        return templates.TemplateResponse(
            request,
            "feedback.html",
            {"event": None, "token": token, "error": "expired", "preselect": ""},
        )
    event = dict(row)
    if event.get("feedback"):
        return templates.TemplateResponse(
            request,
            "feedback.html",
            {"event": event, "token": token, "error": "already_used", "preselect": ""},
        )
    # Prefer form body; fall back to query param (ntfy sends empty body)
    value = feedback or v
    if value not in _VALID_FEEDBACK:
        value = "correct"
    corr = corrected_class.strip() or None
    if value != "wrong_class":
        corr = None
    db.update_feedback(event["id"], value, corr)
    return templates.TemplateResponse(
        request,
        "feedback.html",
        {"event": event, "token": token, "error": "success", "preselect": ""},
    )
