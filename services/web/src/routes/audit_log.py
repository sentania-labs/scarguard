"""Audit log viewer — admin-only read endpoint for audit_events.

Shows login/logout activity, config changes, and user/API-token management
actions recorded by audit.record() in the auth.db audit_events table.

Viewer role is intentionally not granted access: audit rows contain source
IPs and usernames that may be sensitive, and scoping "read-only admin"
access to this surface is a design question better handled in v0.13.x.
"""

from __future__ import annotations

import os
from pathlib import Path

import audit
import auth as auth_module
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from route_auth import require_admin

router = APIRouter(prefix="/admin/audit-log")

_src = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(_src / "templates"))

AUTH_DB_PATH = os.environ.get("AUTH_DB_PATH", "/data/auth.db")

_MAX_LIMIT = 1000


@router.get("", response_class=HTMLResponse)
async def audit_log_view(
    request: Request,
    action: str = "",
    username: str = "",
    limit: int = 200,
) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate

    # Clamp filter inputs so a malicious/typo'd query can't OOM the page
    # or push a huge LIKE pattern into SQLite.
    try:
        limit = max(1, min(int(limit), _MAX_LIMIT))
    except (TypeError, ValueError):
        limit = 200
    action = action[:64]
    username = username[:64]

    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        events = audit.list_events(
            db,
            limit=limit,
            action=action.strip() or None,
            username=username.strip() or None,
        )
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "audit_log.html",
        {
            "events": events,
            "filter_action": action,
            "filter_username": username,
            "limit": limit,
        },
    )
