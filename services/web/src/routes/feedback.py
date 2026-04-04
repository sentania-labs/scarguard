"""Token-based feedback from notification links (unauthenticated)."""

from pathlib import Path

import db
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/feedback")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_VALID_FEEDBACK = ("correct", "false_positive", "wrong_class")


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
