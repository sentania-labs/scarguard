"""ScarGuard web service — FastAPI application entry point."""

from __future__ import annotations

import os
from pathlib import Path

import auth as auth_module
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from routes import about, admin, config, dashboard, events, feed, models, stats, training
from routes import auth as auth_routes
from routes import users as users_routes

app = FastAPI(title="ScarGuard")

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
    auth_module.AUTH_DB_PATH = AUTH_DB_PATH
    auth_module.init_db(AUTH_DB_PATH)


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
