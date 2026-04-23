"""Auth routes — login, logout, and first-run setup."""

from __future__ import annotations

import hmac
import logging
import os

import audit
import auth as auth_module
from config_store import load_cached
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

log = logging.getLogger(__name__)

_src = os.path.dirname(os.path.dirname(__file__))
templates = Jinja2Templates(directory=os.path.join(_src, "templates"))

router = APIRouter()

AUTH_DB_PATH = os.environ.get("AUTH_DB_PATH", "/data/auth.db")

# v1.14 bootstrap-token path. Main.py generates on first boot when no users
# exist; /setup POST verifies and deletes on successful claim.
BOOTSTRAP_TOKEN_PATH = os.environ.get(
    "BOOTSTRAP_TOKEN_PATH", "/data/bootstrap_token",
)


def _read_bootstrap_token() -> str | None:
    """Return the on-disk bootstrap token, or None if absent/empty.

    Env override ``SCARGUARD_BOOTSTRAP_TOKEN`` takes precedence for ops
    pipelines that seed the token out-of-band."""
    override = os.environ.get("SCARGUARD_BOOTSTRAP_TOKEN", "").strip()
    if override:
        return override
    try:
        with open(BOOTSTRAP_TOKEN_PATH) as f:
            val = f.read().strip()
        return val or None
    except FileNotFoundError:
        return None


def _consume_bootstrap_token() -> None:
    """Delete the bootstrap token file. Called after successful /setup.

    Idempotent — missing file is not an error."""
    try:
        os.unlink(BOOTSTRAP_TOKEN_PATH)
        log.info("Bootstrap token consumed; /setup is now permanently closed")
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("Could not delete bootstrap token: %s", exc)


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
async def login_get(request: Request, next: str = "/") -> Response:
    # Already authenticated? Go home.
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": _safe_next(next), "error": None},
    )


@router.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
) -> Response:
    auth_cfg = _get_auth_cfg()
    max_attempts = auth_cfg.get("max_login_attempts", 5)
    lockout_minutes = auth_cfg.get("lockout_duration_minutes", 15)
    session_hours = auth_cfg.get("session_timeout_hours", 24)

    safe_next = _safe_next(next)
    client_ip = request.client.host if request.client else None

    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        # Check lockout before doing anything
        if auth_module.check_lockout(db, username, client_ip, max_attempts, lockout_minutes):
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "next": safe_next,
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
        audit.record(
            db,
            action="login.success" if valid else "login.failure",
            user_id=(user["id"] if valid and user is not None else None),
            username=username,
            client_ip=client_ip,
        )

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
                request,
                "login.html",
                {"next": safe_next, "error": error_msg},
                status_code=401,
            )

        # Create session (valid=True guarantees user is not None)
        assert user is not None
        raw_token = auth_module.create_session(db, user["id"], timeout_hours=session_hours)
        auth_module.purge_expired_sessions(db)
    finally:
        db.close()

    response = RedirectResponse(safe_next, status_code=302)
    response.set_cookie(
        key="session",
        value=raw_token,
        httponly=True,
        samesite="strict",
        secure=_is_https(request),
        max_age=session_hours * 3600,
    )
    return response


# ── Logout ────────────────────────────────────────────────────────────────────

@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    raw_token = request.cookies.get("session")
    if raw_token:
        db = auth_module.get_db(AUTH_DB_PATH)
        try:
            auth_module.delete_session(db, raw_token)
            user = getattr(request.state, "user", None)
            audit.record(
                db,
                action="logout",
                user_id=(user.get("user_id") if user else None),
                username=(user.get("username") if user else None),
                client_ip=(request.client.host if request.client else None),
            )
        finally:
            db.close()

    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session")
    return response


# ── First-run setup ───────────────────────────────────────────────────────────

MIN_PASSWORD_LEN = 12


def _is_common_password(password: str) -> bool:
    """Reject the top bite-sized set of obviously-common passwords.

    A full zxcvbn check would be ideal but adds a dependency for marginal
    benefit on what is already an admin-only surface gated by the
    bootstrap token. This list catches the passwords that appear in
    every ops post-mortem.
    """
    lowered = password.strip().lower()
    common = {
        "password", "password1", "password123", "passw0rd",
        "admin", "administrator", "letmein", "qwerty", "qwerty123",
        "12345678", "123456789", "1234567890", "changeme", "welcome",
        "iloveyou", "monkey", "dragon", "baseball", "football",
        "sunshine", "princess", "scarguard", "scarguard123",
    }
    return lowered in common


def _render_setup(
    request: Request,
    *,
    token: str = "",
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return templates.TemplateResponse(
        request,
        "setup.html",
        {"error": error, "token": token},
        status_code=status_code,
    )


@router.get("/setup", response_class=HTMLResponse)
async def setup_get(request: Request, token: str = "") -> Response:
    # If users already exist, the route is permanently closed.
    if auth_module.users_exist(AUTH_DB_PATH):
        return RedirectResponse("/login", status_code=302)
    # Don't pre-validate the token at GET — showing the form with the
    # token-param-as-hidden-field lets the operator paste the full URL
    # once. Validation happens at POST time.
    return _render_setup(request, token=token)


@router.post("/setup", response_class=HTMLResponse)
async def setup_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    token: str = Form(""),
) -> Response:
    # Closed-route check — users exist.
    if auth_module.users_exist(AUTH_DB_PATH):
        return RedirectResponse("/login", status_code=302)

    # Bootstrap token gate.
    expected = _read_bootstrap_token()
    if not expected:
        log.error("/setup POST attempted but no bootstrap token is available")
        return _render_setup(
            request,
            error="First-run setup is not available. Check the server logs for the bootstrap URL.",
            status_code=403,
        )
    if not hmac.compare_digest(token.strip(), expected):
        log.warning(
            "/setup POST rejected — invalid bootstrap token (client=%s)",
            request.client.host if request.client else "unknown",
        )
        return _render_setup(
            request,
            token=token,
            error="Invalid or missing bootstrap token. Check docker logs for the correct URL.",
            status_code=403,
        )

    # Validate user input
    if not username.strip():
        return _render_setup(
            request, token=token,
            error="Username must not be empty.", status_code=400,
        )
    if len(password) < MIN_PASSWORD_LEN:
        return _render_setup(
            request, token=token,
            error=f"Password must be at least {MIN_PASSWORD_LEN} characters.",
            status_code=400,
        )
    if _is_common_password(password):
        return _render_setup(
            request, token=token,
            error="That password is too common — please pick a less predictable one.",
            status_code=400,
        )
    if password != confirm_password:
        return _render_setup(
            request, token=token,
            error="Passwords do not match.", status_code=400,
        )

    auth_cfg = _get_auth_cfg()
    session_hours = auth_cfg.get("session_timeout_hours", 24)

    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        user_id = auth_module.create_user(db, username.strip(), password, is_admin=True)
        raw_token = auth_module.create_session(db, user_id, timeout_hours=session_hours)
    finally:
        db.close()

    # Consume the bootstrap token — /setup is now permanently closed.
    _consume_bootstrap_token()

    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        key="session",
        value=raw_token,
        httponly=True,
        samesite="strict",
        secure=_is_https(request),
        max_age=session_hours * 3600,
    )
    return response
