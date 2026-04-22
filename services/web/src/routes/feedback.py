"""Token-based feedback from notification links (unauthenticated)."""

import os
from pathlib import Path

import config_store
import db
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from rate_limit_dep import rate_limit

router = APIRouter(prefix="/feedback")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_VALID_FEEDBACK = ("correct", "false_positive", "wrong_class")
_SNAPSHOT_DIR = Path(os.environ.get("SNAPSHOT_DIR", "/data/snapshots"))


def _get_target_classes() -> list[str]:
    """Return target classes from config for the wrong-class picker."""
    return config_store.load_cached().get("detection", {}).get("target_classes", [])


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
    """Show the feedback confirmation page for a detection event.

    Token expiry (7 days, enforced in ``db.get_event_by_token``) is the only
    gate on the form.  Users may re-submit to correct a previous choice —
    e.g. when the first submission picked the wrong class.
    """
    row = db.get_event_by_token(token)
    if row is None:
        return templates.TemplateResponse(
            request,
            "feedback.html",
            {
                "event": None,
                "token": token,
                "error": "expired",
                "preselect": "",
                "target_classes": [],
            },
        )
    event = dict(row)
    preselect = v if v in _VALID_FEEDBACK else ""
    return templates.TemplateResponse(
        request,
        "feedback.html",
        {
            "event": event,
            "token": token,
            "error": "",
            "preselect": preselect,
            "target_classes": _get_target_classes(),
        },
    )


@router.post(
    "/{token}", response_class=HTMLResponse,
    # Unauthenticated route — limit by IP to prevent a spammer hammering
    # every feedback link they can enumerate.
    dependencies=[Depends(rate_limit("feedback-submit", capacity=30, window_seconds=3600))],
)
async def submit_feedback(
    request: Request,
    token: str,
    v: str = "",
    feedback: str = Form(""),
    corrected_class: str = Form(""),
) -> HTMLResponse:
    """Record feedback for a detection event via token.

    Accepts feedback from either form data (web UI) or query param ``?v=``
    (ntfy action buttons POST with empty body).  Re-submission is allowed
    while the token is still valid, so users can correct a wrong first
    choice.  The 7-day token expiry remains the only single-use protection.
    """
    row = db.get_event_by_token(token)
    if row is None:
        return templates.TemplateResponse(
            request,
            "feedback.html",
            {
                "event": None,
                "token": token,
                "error": "expired",
                "preselect": "",
                "target_classes": [],
            },
        )
    event = dict(row)
    # Prefer form body; fall back to query param (ntfy sends empty body)
    value = feedback or v
    if value not in _VALID_FEEDBACK:
        value = "correct"
    corr = corrected_class.strip()[:64] or None
    if value != "wrong_class":
        corr = None
    db.update_feedback(event["id"], value, corr)
    # Reflect the saved values back to the template so the success page shows
    # the user's current choice and allows another correction if needed.
    event["feedback"] = value
    event["corrected_class"] = corr
    return templates.TemplateResponse(
        request,
        "feedback.html",
        {
            "event": event,
            "token": token,
            "error": "success",
            "preselect": "",
            "target_classes": _get_target_classes(),
        },
    )
