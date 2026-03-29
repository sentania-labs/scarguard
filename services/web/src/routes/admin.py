"""Admin routes — service log viewer."""

import asyncio
import logging
import re
import threading
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

log = logging.getLogger(__name__)


def _require_admin(request: Request) -> bool:
    """Return True if the current user is an admin."""
    user = getattr(request.state, "user", None)
    return bool(user and user.get("is_admin"))

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

SERVICES = ["detector", "notifier", "web", "caddy"]
# Must match `name:` in docker-compose.yml. If the project is renamed or run
# with `docker compose -p <name>`, this constant must be updated to match or
# the container lookup will silently return nothing.
COMPOSE_PROJECT = "scarguard"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mK]")

# Check at import time so a missing dependency surfaces in startup logs rather
# than silently on first page visit.
try:
    import docker as _docker_check  # noqa: F401
    _DOCKER_AVAILABLE = True
except ImportError:
    log.warning(
        "Python 'docker' package not installed — Admin Logs tab will be unavailable. "
        "Add 'docker' to services/web/requirements.txt and rebuild the image."
    )
    _DOCKER_AVAILABLE = False


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _find_container(client, service: str) -> object | None:
    """Return the first running container for *service* within this Compose project.

    Scoping by both service name and project name prevents cross-stack matches
    when multiple Compose projects with the same service names run on the host.

    NOTE: /var/run/docker.sock gives host-root equivalent access. This endpoint
    must be protected by authentication (Feature 9) before being exposed beyond
    the local network. The service parameter is validated against SERVICES before
    this function is called, preventing injection via the label filter.
    """
    try:
        containers = client.containers.list(
            filters={
                "label": [
                    f"com.docker.compose.service={service}",
                    f"com.docker.compose.project={COMPOSE_PROJECT}",
                ]
            }
        )
        return containers[0] if containers else None
    except Exception as exc:
        log.warning("Cannot access Docker socket (service=%s): %s", service, exc)
        return None


def _tail_logs_to_queue(
    container,
    q: "asyncio.Queue[str | None]",
    loop: asyncio.AbstractEventLoop,
    tail: int,
    stop: threading.Event,
) -> None:
    """Blocking function — runs in a thread.  Feeds log lines into an async queue."""
    try:
        for chunk in container.logs(stream=True, follow=True, tail=tail):
            if stop.is_set():
                break
            line = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
            asyncio.run_coroutine_threadsafe(q.put(line), loop).result(timeout=5)
    except Exception as exc:
        msg = (
            "[ScarGuard] Log stream stalled (queue full) — reconnect to resume"
            if "TimeoutError" in type(exc).__name__
            else f"[ERROR] {exc}"
        )
        asyncio.run_coroutine_threadsafe(q.put(msg + "\n"), loop).result(timeout=5)
    finally:
        # Use .result() so the sentinel is guaranteed delivered before the thread exits.
        asyncio.run_coroutine_threadsafe(q.put(None), loop).result(timeout=5)


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "logs.html", {"services": SERVICES}
    )


@router.get("/logs/stream")
async def logs_stream(
    request: Request,
    service: str = "detector",
    tail: int = 500,
) -> StreamingResponse:
    """SSE endpoint — streams log lines from a Docker container in real time."""
    if service not in SERVICES:
        service = "detector"
    tail = max(1, min(tail, 5000))

    async def generator():
        loop = asyncio.get_running_loop()

        if not _DOCKER_AVAILABLE:
            yield "data: [ScarGuard] Docker SDK not installed — Admin Logs unavailable.\n\n"
            return

        import docker

        client = docker.from_env()
        try:
            container = await loop.run_in_executor(None, _find_container, client, service)
            if container is None:
                yield (
                    "data: [ScarGuard] Cannot reach Docker socket or container "
                    f"'{service}' is not running. "
                    "Ensure /var/run/docker.sock is mounted in the web service.\n\n"
                )
                return

            q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=500)
            stop = threading.Event()

            t = threading.Thread(
                target=_tail_logs_to_queue,
                args=(container, q, loop, tail, stop),
                name=f"log-tail-{service}",
                daemon=True,
            )
            t.start()

            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        line = await asyncio.wait_for(q.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        # Send a comment to keep the connection alive.
                        yield ": keepalive\n\n"
                        continue

                    if line is None:
                        break

                    clean = _strip_ansi(line).rstrip("\n\r")
                    if clean:
                        # SSE data lines must not contain raw newlines.
                        safe = clean.replace("\n", "  ")
                        yield f"data: {safe}\n\n"
            finally:
                stop.set()
        finally:
            # Closing the client interrupts the blocking container.logs() iterator
            # in the tail thread, causing it to raise and exit promptly rather than
            # waiting indefinitely for the next log chunk.
            try:
                client.close()
            except Exception:
                pass

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Config Backup Management ─────────────────────────────────────────────────


@router.get("/backups", response_class=HTMLResponse)
async def backups_page(request: Request) -> Response:
    """List all config backups."""
    if not _require_admin(request):
        return RedirectResponse("/", status_code=302)
    from main import backup_manager

    backups = backup_manager.list_backups() if backup_manager else []
    return templates.TemplateResponse(request, "backups.html", {"backups": backups})


@router.get("/backups/{name}/diff")
async def backup_diff(request: Request, name: str) -> JSONResponse:
    """Return a unified diff between a backup and the current config."""
    if not _require_admin(request):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    from main import backup_manager

    if not backup_manager:
        return JSONResponse(
            {"error": "Backup manager not initialized"}, status_code=500
        )
    if not name.startswith("scarguard_") or not name.endswith(".yml"):
        return JSONResponse({"error": "Invalid backup name"}, status_code=400)
    diff = backup_manager.get_diff(name)
    if diff is None:
        return JSONResponse({"error": "Backup not found"}, status_code=404)
    return JSONResponse({"diff": diff})


@router.post("/backups/{name}/restore")
async def backup_restore(request: Request, name: str) -> JSONResponse:
    """Restore a backup to the active config."""
    if not _require_admin(request):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    from main import backup_manager

    if not backup_manager:
        return JSONResponse(
            {"error": "Backup manager not initialized"}, status_code=500
        )
    if not name.startswith("scarguard_") or not name.endswith(".yml"):
        return JSONResponse({"error": "Invalid backup name"}, status_code=400)
    ok = backup_manager.restore(name)
    if not ok:
        return JSONResponse({"error": "Restore failed"}, status_code=500)
    return JSONResponse({"ok": True, "message": f"Restored from {name}"})


@router.post("/backups/create")
async def backup_create(request: Request) -> JSONResponse:
    """Create a manual config backup."""
    if not _require_admin(request):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    from main import backup_manager

    if not backup_manager:
        return JSONResponse(
            {"error": "Backup manager not initialized"}, status_code=500
        )
    filename = backup_manager.create_backup("manual")
    if not filename:
        return JSONResponse({"error": "Failed to create backup"}, status_code=500)
    return JSONResponse({"ok": True, "filename": filename})
