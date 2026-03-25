"""Auth routes — login, logout, and first-run setup."""

from __future__ import annotations

import os

import auth as auth_module
from config_store import load_cached
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

_src = os.path.dirname(os.path.dirname(__file__))
templates = Jinja2Templates(directory=os.path.join(_src, "templates"))

router = APIRouter()

AUTH_DB_PATH = os.environ.get("AUTH_DB_PATH", "/data/auth.db")


def _get_auth_cfg() -> dict:
    cfg = load_cached()
    return cfg.get("system", {}).get("auth", {})


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


# ── Login ─────────────────────────────────────────────────────────────────────

def _safe_next(value: str) -> str:
    """Sanitize a redirect-next value: must be a relative path, not protocol-relative."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request, next: str = "/") -> HTMLResponse:
    # Already authenticated? Go home.
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse("/", status_code=302)  # type: ignore[return-value]
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "next": _safe_next(next), "error": None},
    )


@router.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
) -> HTMLResponse:
    auth_cfg = _get_auth_cfg()
    max_attempts = auth_cfg.get("max_login_attempts", 5)
    lockout_minutes = auth_cfg.get("lockout_duration_minutes", 15)
    session_hours = auth_cfg.get("session_timeout_hours", 24)

    client_ip = request.client.host if request.client else None

    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        # Check lockout before doing anything
        if auth_module.check_lockout(db, username, client_ip, max_attempts, lockout_minutes):
            return templates.TemplateResponse(
                "login.html",
                {
                    "request": request,
                    "next": next,
                    "error": (
                        f"Account locked after too many failed attempts. "
                        f"Try again in {lockout_minutes} minutes."
                    ),
                },
                status_code=429,
            )

        user = auth_module.get_user(db, username)
        valid = (
            user is not None
            and not user["disabled"]
            and auth_module.verify_password(password, user["password_hash"])
        )

        auth_module.record_attempt(db, username, client_ip, success=valid)

        if not valid:
            cutoff = auth_module._utcnow_minus(minutes=lockout_minutes)
            failed_count = db.execute(
                """SELECT COUNT(*) FROM login_attempts
                   WHERE username=? AND success=0 AND attempted_at > ?""",
                (username, cutoff),
            ).fetchone()[0]
            remaining = max(0, max_attempts - failed_count)
            error_msg = "Invalid username or password."
            if remaining <= 2:
                error_msg += f" {remaining} attempt(s) remaining before lockout."
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "next": next, "error": error_msg},
                status_code=401,
            )

        # Create session
        raw_token = auth_module.create_session(db, user["id"], timeout_hours=session_hours)
        auth_module.purge_expired_sessions(db)
    finally:
        db.close()

    safe_next = _safe_next(next)

    response = RedirectResponse(safe_next, status_code=302)
    response.set_cookie(
        key="session",
        value=raw_token,
        httponly=True,
        samesite="strict",
        secure=_is_https(request),
        max_age=session_hours * 3600,
    )
    return response  # type: ignore[return-value]


# ── Logout ────────────────────────────────────────────────────────────────────

@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    raw_token = request.cookies.get("session")
    if raw_token:
        db = auth_module.get_db(AUTH_DB_PATH)
        try:
            auth_module.delete_session(db, raw_token)
        finally:
            db.close()

    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session")
    return response


# ── First-run setup ───────────────────────────────────────────────────────────

@router.get("/setup", response_class=HTMLResponse)
async def setup_get(request: Request) -> HTMLResponse:
    # If users already exist, redirect to login
    if auth_module.users_exist(AUTH_DB_PATH):
        return RedirectResponse("/login", status_code=302)  # type: ignore[return-value]
    return templates.TemplateResponse(
        "setup.html",
        {"request": request, "error": None},
    )


@router.post("/setup", response_class=HTMLResponse)
async def setup_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
) -> HTMLResponse:
    # If users already exist, deny
    if auth_module.users_exist(AUTH_DB_PATH):
        return RedirectResponse("/login", status_code=302)  # type: ignore[return-value]

    # Validate input
    if not username.strip():
        return templates.TemplateResponse(
            "setup.html",
            {"request": request, "error": "Username must not be empty."},
            status_code=400,
        )
    if len(password) < 8:
        return templates.TemplateResponse(
            "setup.html",
            {"request": request, "error": "Password must be at least 8 characters."},
            status_code=400,
        )
    if password != confirm_password:
        return templates.TemplateResponse(
            "setup.html",
            {"request": request, "error": "Passwords do not match."},
            status_code=400,
        )

    auth_cfg = _get_auth_cfg()
    session_hours = auth_cfg.get("session_timeout_hours", 24)

    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        user_id = auth_module.create_user(db, username.strip(), password, is_admin=True)
        raw_token = auth_module.create_session(db, user_id, timeout_hours=session_hours)
    finally:
        db.close()

    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        key="session",
        value=raw_token,
        httponly=True,
        samesite="strict",
        secure=_is_https(request),
        max_age=session_hours * 3600,
    )
    return response  # type: ignore[return-value]
