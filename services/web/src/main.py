"""ScarGuard web service — FastAPI application entry point."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from urllib.parse import parse_qs

import auth as auth_module
from config_backup import ConfigBackupManager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from routes import (
    about,
    actuations,
    admin,
    audit_log,
    config,
    dashboard,
    deterrent,
    events,
    feedback,
    models,
    snapshot,
    stats,
    training,
)
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
    _ensure_secret_key()
    _ensure_bootstrap_token()
    _migrate_retention_fields()
    _migrate_base_url_to_domain()
    _encrypt_plaintext_secrets()
    backup_manager = ConfigBackupManager()
    backup_manager.start()


def _ensure_bootstrap_token() -> None:
    """Generate a one-time token gating the /setup route.

    Before v1.14 any network-reachable caller could claim the first admin
    account on a fresh deployment. The token closes that window: it's
    generated on the first web startup where no users exist, written to
    /data/bootstrap_token (chmod 600), and logged to stdout so the
    operator can grab it from ``docker compose logs web``. /setup POST
    verifies it and deletes the file on success, making the route
    permanently inactive afterwards.

    If users already exist, there is nothing to guard — skip."""
    import logging

    log = logging.getLogger("startup")
    if auth_module.users_exist(AUTH_DB_PATH):
        return

    from routes.auth import BOOTSTRAP_TOKEN_PATH

    try:
        if os.path.exists(BOOTSTRAP_TOKEN_PATH):
            with open(BOOTSTRAP_TOKEN_PATH) as f:
                token = f.read().strip()
        else:
            token = secrets.token_urlsafe(32)
            parent = os.path.dirname(BOOTSTRAP_TOKEN_PATH)
            if parent:
                os.makedirs(parent, exist_ok=True)
            fd = os.open(BOOTSTRAP_TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, token.encode("ascii"))
            finally:
                os.close(fd)

        log.warning(
            "═══════════════════════════════════════════════════════════\n"
            "  First-run setup — complete within 24 hours:\n"
            "    Browse to: /setup?token=%s\n"
            "  Token also stored at %s (chmod 600).\n"
            "═══════════════════════════════════════════════════════════",
            token, BOOTSTRAP_TOKEN_PATH,
        )
    except Exception as exc:
        log.error("Failed to generate bootstrap token: %s", exc)


def _ensure_secret_key() -> None:
    """Generate the at-rest encryption key on first boot.

    The web service is the canonical writer of this key — notifier and
    deterrent only read it. Web has /data RW, so it can create the file
    with chmod 600. If the key already exists, this is a no-op."""
    import logging

    import secret_box
    log = logging.getLogger("startup")
    try:
        if secret_box.write_key_if_missing():
            log.info(
                "Created %s for at-rest secret encryption (chmod 600)",
                secret_box.DEFAULT_KEY_PATH,
            )
    except Exception as exc:
        log.error("Failed to create secret key: %s", exc)


def _encrypt_plaintext_secrets() -> None:
    """One-shot migration: if the on-disk config still has plaintext
    sensitive fields, re-save once so they get encrypted. Subsequent saves
    do this transparently via config_store.save."""
    import logging

    import config_store
    import secret_box
    log = logging.getLogger("startup")
    try:
        key = secret_box.try_load_key()
        if key is None:
            log.warning(
                "No secret key available — secrets remain plaintext on disk",
            )
            return
        cfg = config_store.load()
        if not secret_box.has_plaintext_secrets(cfg):
            return
        log.warning(
            "Plaintext secrets detected in scarguard.yml — encrypting on disk now",
        )
        config_store.save(cfg)
    except Exception as exc:
        log.error("Plaintext secret migration failed: %s", exc)


def _migrate_retention_fields() -> None:
    """One-time migration: consolidate legacy snapshot_retention_days and
    metrics_retention_days into a single retention_days field.

    TODO: Remove this migration once enough releases have passed (target x.14.x).
    """
    import logging

    import config_store
    log = logging.getLogger("startup")
    try:
        cfg = config_store.load()
        sys_cfg = cfg.get("system", {})
        if not isinstance(sys_cfg, dict):
            return

        old_snap = sys_cfg.get("snapshot_retention_days")
        old_metrics = sys_cfg.get("metrics_retention_days")
        has_new = "retention_days" in sys_cfg

        if old_snap is None and old_metrics is None:
            return  # Nothing to migrate

        if has_new:
            # New key already present; just clean up old keys
            changed = False
            if old_snap is not None:
                del sys_cfg["snapshot_retention_days"]
                changed = True
            if old_metrics is not None:
                del sys_cfg["metrics_retention_days"]
                changed = True
            if changed:
                config_store.save(cfg)
                log.info("Removed legacy retention keys (retention_days already set)")
            return

        # Derive new value: take the larger of the two old values
        values = [int(v) for v in (old_snap, old_metrics) if v is not None]
        retention_days = max(values)

        sys_cfg["retention_days"] = retention_days
        if old_snap is not None:
            del sys_cfg["snapshot_retention_days"]
        if old_metrics is not None:
            del sys_cfg["metrics_retention_days"]

        config_store.save(cfg)
        log.info(
            "Migrated snapshot_retention_days/metrics_retention_days → retention_days: %d",
            retention_days,
        )
    except Exception as exc:
        log.warning("Retention fields migration skipped: %s", exc)


def _migrate_base_url_to_domain() -> None:
    """One-time migration: move system.base_url → tls.domain.

    v0.12.4 removed the separate base_url field.  If the user had base_url
    set but tls.domain is empty, extract the hostname and populate tls.domain
    so notification feedback links keep working.

    TODO: Remove this migration once enough releases have passed (target x.15.x).
    """
    import logging
    from urllib.parse import urlparse

    import config_store
    log = logging.getLogger("startup")
    try:
        cfg = config_store.load()
        sys_cfg = cfg.get("system", {})
        if not isinstance(sys_cfg, dict):
            return
        base_url = sys_cfg.get("base_url", "")
        if not base_url:
            return

        tls_cfg = cfg.get("tls", {})
        if not isinstance(tls_cfg, dict):
            tls_cfg = {}
            cfg["tls"] = tls_cfg

        if not tls_cfg.get("domain"):
            parsed = urlparse(base_url)
            domain = parsed.hostname or ""
            if domain:
                tls_cfg["domain"] = domain
                log.info("Migrated system.base_url → tls.domain: %s", domain)

        # Only remove base_url if domain was already set or successfully migrated
        if tls_cfg.get("domain"):
            del sys_cfg["base_url"]
            config_store.save(cfg)
            log.info("Removed deprecated system.base_url")
        else:
            log.warning("Could not extract domain from base_url %r — keeping it as fallback", base_url)
    except Exception as exc:
        log.warning("base_url → domain migration skipped: %s", exc)


# ── Auth middleware ────────────────────────────────────────────────────────────

# Paths that are always public (no login required).
_PUBLIC_PREFIXES = ("/login", "/setup", "/static/", "/health", "/feedback")


def _is_public(path: str) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p) for p in _PUBLIC_PREFIXES)


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept


def _parse_auth_enabled(value: object) -> bool:
    """Parse the ``system.auth.enabled`` config value safely.

    A naive ``bool(value)`` cast would report ``"false"`` as truthy,
    which silently keeps auth on when an operator quoted the value in
    hand-edited YAML.  Accept real bools plus the usual false-ish string
    spellings.  Default (missing value) is True — fail closed.
    """
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(value)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Default: no authenticated user. Always set so templates can safely read it.
    request.state.user = None
    request.state.deterrent_enabled = False  # set properly below when config loads

    # Always allow public paths (login page, setup page, static assets)
    if _is_public(path):
        return await call_next(request)

    # Load auth config (cached by config_store)
    from config_store import load_cached  # local import to avoid circular at module level
    cfg = load_cached()
    system_cfg = cfg.get("system") or {}
    auth_cfg = system_cfg.get("auth") or {}
    auth_enabled = _parse_auth_enabled(auth_cfg.get("enabled", True))

    # Expose deterrent state to base.html nav (controls Deterrent link visibility)
    act_cfg = cfg.get("deterrent") or {}
    request.state.deterrent_enabled = bool(act_cfg.get("enabled", False))

    if not auth_enabled:
        # When auth is disabled (single-user / dev / test setups), grant every
        # request full admin access so the role-based route guards introduced
        # in v0.12.7 don't lock the operator out.  This preserves the pre-
        # v0.12.7 behaviour where "auth disabled" meant "no checks at all".
        request.state.user = {
            "user_id": 0,
            "username": "anonymous",
            "role": "admin",
            "is_admin": 1,
            "disabled": 0,
        }
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

# v1.14: HMAC key persisted to /data so it survives process restart and
# survives a future scale to >1 web replica. Pre-v1.14 it regenerated each
# process start, which silently invalidated every active session on every
# restart and would have broken multi-replica deployments outright.
_CSRF_SECRET_PATH = os.environ.get("CSRF_SECRET_PATH", "/data/csrf_secret")


def _load_or_create_csrf_secret() -> str:
    if os.path.exists(_CSRF_SECRET_PATH):
        try:
            with open(_CSRF_SECRET_PATH) as f:
                val = f.read().strip()
            if val:
                return val
        except Exception:
            # fall through and regenerate
            pass
    fresh = secrets.token_hex(32)
    parent = os.path.dirname(_CSRF_SECRET_PATH)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception:
            return fresh
    try:
        fd = os.open(_CSRF_SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, fresh.encode("ascii"))
        finally:
            os.close(fd)
    except Exception:
        # If /data isn't writable yet, fall back to in-memory — better than
        # crashing. Subsequent restart will retry persistence.
        pass
    return fresh


_csrf_secret: str = _load_or_create_csrf_secret()


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

    # Skip CSRF for token-based feedback (the token itself is the auth)
    if request.url.path.startswith("/feedback/"):
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

        # Require token via header or form field, and verify it matches cookie.
        # Token can come from X-CSRF-Token header (htmx/fetch) or
        # _csrf_token form field (plain HTML forms).
        #
        # IMPORTANT: We must NOT call request.form() here — doing so in
        # BaseHTTPMiddleware consumes the body stream, preventing downstream
        # route handlers from reading Form() fields.  Instead, parse the raw
        # body bytes (which Starlette caches without breaking form parsing).
        submitted_token: str | None = request.headers.get("x-csrf-token")
        if submitted_token is None:
            content_type = request.headers.get("content-type", "")
            body = await request.body()
            if "application/x-www-form-urlencoded" in content_type:
                params = parse_qs(body.decode())
                raw = params.get("_csrf_token", [None])[0]
                submitted_token = raw if isinstance(raw, str) else None
            elif "multipart/form-data" in content_type:
                # Extract _csrf_token from multipart body without calling
                # request.form().  The token field is always a short text
                # value placed by the hidden input, so a byte search works.
                marker = b'name="_csrf_token"\r\n\r\n'
                idx = body.find(marker)
                if idx != -1:
                    start = idx + len(marker)
                    end = body.find(b"\r\n", start)
                    try:
                        raw = body[start:end].decode() if end != -1 else None
                    except UnicodeDecodeError:
                        raw = None
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


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ── Routes ─────────────────────────────────────────────────────────────────────
app.include_router(auth_routes.router)
app.include_router(users_routes.router)
app.include_router(dashboard.router)
app.include_router(events.router)
app.include_router(config.router)
app.include_router(models.router)
app.include_router(about.router)
app.include_router(admin.router)
app.include_router(stats.router)
app.include_router(training.router)
app.include_router(audit_log.router)
app.include_router(snapshot.router)
app.include_router(feedback.router)
app.include_router(deterrent.router)
app.include_router(actuations.router)
