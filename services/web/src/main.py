"""ScarGuard web service — FastAPI application entry point."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

import auth as auth_module
from config_backup import ConfigBackupManager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from routes import about, admin, config, dashboard, events, feed, models, snapshot, stats, training
from routes import auth as auth_routes
from routes import users as users_routes

app = FastAPI(title="ScarGuard")

backup_manager: ConfigBackupManager | None = None

# ── Static assets ──────────────────────────────────────────────────────────────
_src = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(_src / "static")), name="static")

# Snapshots and model files live on shared volumes; serve them directly.
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/data/snapshots")
MODELS_DIR = os.environ.get("MODELS_DIR", "/models")
AUTH_DB_PATH = os.environ.get("AUTH_DB_PATH", "/data/auth.db")

Path(SNAPSHOT_DIR).mkdir(parents=True, exist_ok=True)
Path(MODELS_DIR).mkdir(parents=True, exist_ok=True)

app.mount("/snapshots", StaticFiles(directory=SNAPSHOT_DIR), name="snapshots")
app.mount("/model-files", StaticFiles(directory=MODELS_DIR), name="model-files")

# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def _startup() -> None:
    global backup_manager
    auth_module.AUTH_DB_PATH = AUTH_DB_PATH
    auth_module.init_db(AUTH_DB_PATH)
    _migrate_ssl_to_tls()
    backup_manager = ConfigBackupManager()
    backup_manager.start()


def _migrate_ssl_to_tls() -> None:
    """One-time migration: convert legacy ssl section to new tls section.

    TODO(v1.0): Remove this migration and the legacy ssl fallback in
    caddy-entrypoint.sh once enough releases have passed (target v1.0 or v0.12).
    """
    import logging

    import config_store
    log = logging.getLogger("startup")
    try:
        cfg = config_store.load()
        ssl_cfg = cfg.get("ssl", {})
        if cfg.get("tls") or not isinstance(ssl_cfg, dict) or not ssl_cfg.get("enabled"):
            return  # Already migrated or no legacy SSL
        tls_cfg = {
            "mode": "manual",
            "domain": "",
            "cert_path": ssl_cfg.get("cert_path", "/config/certs/cert.pem"),
            "key_path": ssl_cfg.get("key_path", "/config/certs/key.pem"),
        }
        cfg["tls"] = tls_cfg
        del cfg["ssl"]
        config_store.save(cfg)
        log.info(
            "Migrated legacy ssl config to tls (mode=manual, cert=%s)",
            tls_cfg["cert_path"],
        )
    except Exception as exc:
        log.warning("SSL→TLS migration skipped: %s", exc)


# ── Auth middleware ────────────────────────────────────────────────────────────

# Paths that are always public (no login required).
_PUBLIC_PREFIXES = ("/login", "/setup", "/static/")


def _is_public(path: str) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p) for p in _PUBLIC_PREFIXES)


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Default: no authenticated user. Always set so templates can safely read it.
    request.state.user = None

    # Always allow public paths (login page, setup page, static assets)
    if _is_public(path):
        return await call_next(request)

    # Load auth config (cached by config_store)
    from config_store import load_cached  # local import to avoid circular at module level
    cfg = load_cached()
    auth_cfg = cfg.get("system", {}).get("auth", {})
    auth_enabled = auth_cfg.get("enabled", True)

    if not auth_enabled:
        return await call_next(request)

    # First-run: no users exist → redirect to setup page
    if not auth_module.users_exist(AUTH_DB_PATH):
        return RedirectResponse("/setup", status_code=302)

    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        user = None

        # 1. Check session cookie
        raw_session = request.cookies.get("session")
        if raw_session:
            user = auth_module.validate_session(db, raw_session)

        # 2. Check Bearer token (always checked, but only enforced when require_api_auth=True)
        if user is None:
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                raw_token = auth_header[7:]
                token_user = auth_module.validate_api_token(db, raw_token)
                require_api_auth = auth_cfg.get("require_api_auth", False)
                if token_user is not None:
                    user = token_user
                elif require_api_auth:
                    # Token provided but invalid, and API auth is required
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

        if user is not None:
            request.state.user = user
            return await call_next(request)

        # Not authenticated
        if _wants_html(request):
            return RedirectResponse(f"/login?next={request.url.path}", status_code=302)
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    finally:
        db.close()


# ── CSRF protection ───────────────────────────────────────────────────────────
#
# Signed double-submit cookie pattern.  A signed token is set in a non-httponly
# cookie (so JS can read it).  Mutating requests must echo the token back via
# either a hidden form field (_csrf_token) or an X-CSRF-Token header.
#
# The token is HMAC-signed so an attacker cannot forge one.  The cookie is
# SameSite=Strict so cross-site requests never include it.  Together these
# provide defense-in-depth against CSRF.
#
# API requests using Bearer auth are exempt (no cookie = no CSRF risk).

_CSRF_COOKIE = "csrf_token"
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Secret key for HMAC — generated once per process start.
_csrf_secret: str = secrets.token_hex(32)


def _generate_csrf_token() -> str:
    """Generate a CSRF token: random nonce + HMAC signature."""
    nonce = secrets.token_hex(16)
    sig = hmac.new(_csrf_secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{nonce}.{sig}"


def _verify_csrf_token(token: str) -> bool:
    """Verify that a CSRF token was generated by this process."""
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False
    nonce, sig = parts
    expected = hmac.new(_csrf_secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()[:16]
    return hmac.compare_digest(sig, expected)


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    # Skip CSRF for API requests using Bearer auth (no cookie = no CSRF risk)
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        request.state.csrf_token = ""
        return await call_next(request)

    # Ensure a CSRF cookie exists and is valid
    csrf_cookie = request.cookies.get(_CSRF_COOKIE)
    needs_cookie = not csrf_cookie or not _verify_csrf_token(csrf_cookie)
    if needs_cookie:
        csrf_cookie = _generate_csrf_token()

    # Validate on mutating requests.
    # The token must be submitted via X-CSRF-Token header (htmx/fetch) or
    # _csrf_token form field (plain HTML forms), AND must match the cookie.
    # The cookie's SameSite=Strict attribute prevents cross-site requests
    # from including it at all.  Requiring a matching submitted token on top
    # of the signed cookie provides defense-in-depth.
    if request.method not in _CSRF_SAFE_METHODS:
        submitted_cookie = request.cookies.get(_CSRF_COOKIE)

        # Require a valid CSRF cookie (SameSite=Strict blocks cross-site)
        if not submitted_cookie or not _verify_csrf_token(submitted_cookie):
            if _wants_html(request):
                return RedirectResponse(
                    f"/login?next={request.url.path}", status_code=302
                )
            return JSONResponse({"error": "CSRF validation failed"}, status_code=403)

        # Require token via header or form field, and verify it matches cookie
        # Token can come from X-CSRF-Token header (htmx/fetch) or
        # _csrf_token form field (plain HTML forms).  The form body is
        # cached by Starlette, so route handlers can still read it.
        submitted_token: str | None = request.headers.get("x-csrf-token")
        if submitted_token is None:
            content_type = request.headers.get("content-type", "")
            if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
                form = await request.form()
                raw = form.get("_csrf_token")
                submitted_token = raw if isinstance(raw, str) else None

        if submitted_token is None or not hmac.compare_digest(submitted_token, submitted_cookie):
            if _wants_html(request):
                return RedirectResponse(
                    f"/login?next={request.url.path}", status_code=302
                )
            return JSONResponse({"error": "CSRF validation failed"}, status_code=403)

    # Inject token into request state so templates can access it
    request.state.csrf_token = csrf_cookie

    response = await call_next(request)

    # Set or refresh the CSRF cookie
    if needs_cookie:
        response.set_cookie(
            key=_CSRF_COOKIE,
            value=csrf_cookie,
            httponly=False,  # JS needs to read it for fetch headers
            samesite="strict",
            secure=request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https",
            path="/",
        )

    return response


# ── Routes ─────────────────────────────────────────────────────────────────────
app.include_router(auth_routes.router)
app.include_router(users_routes.router)
app.include_router(dashboard.router)
app.include_router(events.router)
app.include_router(config.router)
app.include_router(models.router)
app.include_router(feed.router)
app.include_router(about.router)
app.include_router(admin.router)
app.include_router(stats.router)
app.include_router(training.router)
app.include_router(snapshot.router)
