"""ScarGuard config-API — write-path endpoints for config mutations.

This is the optional config-write service, active only when the
``config-api`` compose profile is enabled.  It exposes the 8 POST
endpoints that mutate scarguard.yml or trigger side-effects (backups,
test notifications).

v1.15 scaffold: all routes return 501 Not Implemented.  The actual
handler logic will be migrated from the web service in a follow-up PR.
"""

from __future__ import annotations

import logging
import os
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

app = FastAPI(title="ScarGuard Config API")

_VERSION = os.environ.get("VERSION", "local-dev")
_GIT_COMMIT = os.environ.get("GIT_COMMIT", "unknown")


# ── Health ──────────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check — returns 200 when the service is running."""
    return {"status": "ok", "service": "config-api", "version": _VERSION}


# ── Placeholder write endpoints ─────────────────────────────────────────────────
#
# These 8 routes mirror the config-write POSTs currently handled by the web
# service.  They return 501 until the handler logic is migrated.


def _not_implemented(endpoint: str) -> JSONResponse:
    """Standard 501 response for scaffold endpoints."""
    return JSONResponse(
        {
            "ok": False,
            "error": "Not implemented — config-api route migration pending",
            "endpoint": endpoint,
        },
        status_code=501,
    )


@app.post("/config/structured")
async def save_structured_config(request: Request) -> Response:
    """Structured config save — migrating from web service."""
    return _not_implemented("/config/structured")


@app.post("/config")
async def save_config(request: Request) -> Response:
    """Raw YAML config save — migrating from web service."""
    return _not_implemented("/config")


@app.post("/config/tls/upload-cert")
async def upload_tls_cert(request: Request) -> Response:
    """TLS certificate upload — migrating from web service."""
    return _not_implemented("/config/tls/upload-cert")


@app.post("/config/test-notification")
async def send_test_notification(request: Request) -> Response:
    """Test notification trigger — migrating from web service."""
    return _not_implemented("/config/test-notification")


@app.post("/admin/deterrent")
async def save_deterrent(request: Request) -> Response:
    """Deterrent config save — migrating from web service."""
    return _not_implemented("/admin/deterrent")


@app.post("/admin/backups/{name}/restore")
async def backup_restore(request: Request, name: str) -> Response:
    """Config backup restore — migrating from web service."""
    return _not_implemented(f"/admin/backups/{name}/restore")


@app.post("/admin/backups/create")
async def backup_create(request: Request) -> Response:
    """Config backup create — migrating from web service."""
    return _not_implemented("/admin/backups/create")


@app.post("/admin/db-backups/trigger")
async def trigger_db_backup(request: Request) -> Response:
    """DB backup trigger — migrating from web service."""
    return _not_implemented("/admin/db-backups/trigger")
