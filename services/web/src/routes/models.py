import json
import logging
import os
import tempfile
import time as _time
import uuid
from pathlib import Path
from typing import Any

import config_store
import redis.asyncio as aioredis
from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from route_auth import require_admin, require_viewer
from starlette.responses import Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/models"))
ALLOWED_EXTENSIONS = {".pt", ".engine", ".onnx"}
_DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
UPLOAD_CHUNK_SIZE = int(os.environ.get("MODEL_UPLOAD_CHUNK_SIZE", str(_DEFAULT_CHUNK_SIZE)))
if UPLOAD_CHUNK_SIZE <= 0:
    raise ValueError(
        f"MODEL_UPLOAD_CHUNK_SIZE must be a positive integer (got {UPLOAD_CHUNK_SIZE}); "
        f"default is {_DEFAULT_CHUNK_SIZE} bytes"
    )
MAX_UPLOAD_BYTES = int(os.environ["MODEL_UPLOAD_MAX_BYTES"]) if "MODEL_UPLOAD_MAX_BYTES" in os.environ else None

_CLASSES_REQUEST_CHANNEL = "scarguard:model.classes.request"
_CLASSES_RESPONSE_PREFIX = "scarguard:model.classes.response:"
_CLASSES_TIMEOUT_SEC = 15.0

# Cache of detector RPC results keyed on (abs_path, mtime_ns, size).  Size is
# included to defeat in-place rewrites that preserve mtime (e.g. ``os.replace``
# from upload_model, rsync ``--times``).  Detector has its own cache too;
# this web-side cache short-circuits the pub/sub round-trip when a user
# re-opens the Models page or the Config chip picker re-renders before
# detector's cache sees the request.
_classes_cache: dict[tuple[str, int, int], dict[str, Any]] = {}
_CLASSES_CACHE_MAX = 32


@router.get("", response_class=HTMLResponse)
async def models_page(request: Request, uploaded: str = "") -> Response:
    """Model management page. Readable by viewer + admin; uploads are admin-only."""
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate
    files = sorted(
        [
            {"name": f.name, "size_mb": round(f.stat().st_size / 1_048_576, 1)}
            for f in MODELS_DIR.iterdir()
            if f.is_file() and f.suffix in ALLOWED_EXTENSIONS
        ],
        key=lambda x: str(x["name"]),
    )
    return templates.TemplateResponse(
        request,
        "models.html",
        {"files": files, "uploaded": uploaded, "error": None},
    )


@router.post("", response_class=HTMLResponse)
async def upload_model(request: Request, file: UploadFile = File(...)) -> Response:
    """Upload a new model file. Admin only — writes to shared /models volume."""
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        files = _list_files()
        return templates.TemplateResponse(
            request,
            "models.html",
            {
                "files": files,
                "uploaded": "",
                "error": f"Unsupported file type '{suffix}'. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
            },
        )

    dest = MODELS_DIR / Path(filename).name
    temp_file_path: Path | None = None
    bytes_written = 0

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=MODELS_DIR,
            prefix=f".{dest.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_file_path = Path(temp_file.name)
            while True:
                chunk = await file.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if MAX_UPLOAD_BYTES is not None and bytes_written > MAX_UPLOAD_BYTES:
                    temp_file.close()
                    temp_file_path.unlink(missing_ok=True)
                    files = _list_files()
                    max_size_mb = round(MAX_UPLOAD_BYTES / 1_048_576, 1)
                    return templates.TemplateResponse(
                        request,
                        "models.html",
                        {
                            "files": files,
                            "uploaded": "",
                            "error": f"Upload exceeds max size of {max_size_mb} MB.",
                        },
                    )
                temp_file.write(chunk)

        os.replace(temp_file_path, dest)
    finally:
        await file.close()
        if temp_file_path and temp_file_path.exists() and temp_file_path != dest:
            temp_file_path.unlink(missing_ok=True)

    # Invalidate cached class lists for this filename — a re-upload may have
    # changed the embedded names (P1-2).  We can't easily reach the detector's
    # cache from here, but its key includes file size; if size changes the
    # detector misses too.  For matching size+mtime collisions, the detector
    # would still serve stale — accepted as a known edge case until a Redis
    # invalidate channel is added (tracked under v0.14 future ideas).
    dest_str = str(dest.resolve())
    for key in list(_classes_cache.keys()):
        if key[0] == dest_str:
            _classes_cache.pop(key, None)

    return RedirectResponse(url=f"/models?uploaded={file.filename}", status_code=303)


def _list_files() -> list[dict]:
    return sorted(
        [
            {"name": f.name, "size_mb": round(f.stat().st_size / 1_048_576, 1)}
            for f in MODELS_DIR.iterdir()
            if f.is_file() and f.suffix in ALLOWED_EXTENSIONS
        ],
        key=lambda x: str(x["name"]),
    )


def _safe_resolve_model(filename: str) -> Path | None:
    """Resolve *filename* to a known model file via whitelist lookup.

    Rather than validate *filename* and then construct a path from it
    (which CodeQL correctly flags as user-data-in-path-expression even
    with a regex gate — the taint tracker can't follow sanitisation
    through Path joins), we enumerate the files actually present in
    ``MODELS_DIR`` and treat *filename* as a dict key against that
    known-safe set.  The returned ``Path`` therefore always comes from
    a trusted directory listing — the user-supplied string never reaches
    a filesystem sink.
    """
    if not isinstance(filename, str) or not filename:
        return None
    # Cheap early-reject of obviously-bad shapes before we do the listing.
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        return None
    known: dict[str, Path] = {}
    try:
        for entry in MODELS_DIR.iterdir():
            if entry.is_file() and entry.suffix.lower() in ALLOWED_EXTENSIONS:
                known[entry.name] = entry
    except OSError:
        return None
    return known.get(filename)


def _redis_params() -> dict[str, Any]:
    cfg = config_store.load_cached()
    redis_cfg = cfg.get("redis", {})
    return {
        "host": redis_cfg.get("host", "redis"),
        "port": int(redis_cfg.get("port", 6379)),
        "password": os.environ.get("REDIS_PASSWORD", "") or None,
        "decode_responses": True,
    }


async def _fetch_model_classes_via_redis(model_path: str) -> dict[str, Any]:
    """Publish a class-list request and await the detector's reply."""
    request_id = uuid.uuid4().hex
    result_channel = f"{_CLASSES_RESPONSE_PREFIX}{request_id}"
    payload = {"request_id": request_id, "model_path": model_path}

    client = aioredis.Redis(**_redis_params())
    pubsub = client.pubsub()
    try:
        await pubsub.subscribe(result_channel)
        await client.publish(_CLASSES_REQUEST_CHANNEL, json.dumps(payload))

        deadline = _time.monotonic() + _CLASSES_TIMEOUT_SEC
        while _time.monotonic() < deadline:
            remaining = max(0.5, deadline - _time.monotonic())
            msg = await pubsub.get_message(timeout=min(remaining, 2.0))
            if msg and msg["type"] == "message":
                try:
                    reply = json.loads(msg["data"])
                except (json.JSONDecodeError, TypeError):
                    continue
                # Defence in depth: the reply channel is already per-request
                # (``{prefix}{request_id}``), but verify the payload's
                # request_id matches before trusting it — a stale publisher
                # or a shared-channel misroute could otherwise poison the
                # cache with someone else's result.
                if not isinstance(reply, dict) or reply.get("request_id") != request_id:
                    continue
                return reply
        return {
            "ok": False,
            "error": "Request timed out — detector may not be running",
        }
    finally:
        try:
            await pubsub.unsubscribe(result_channel)
        except Exception:
            pass
        await client.close()


@router.get("/{filename}/classes", response_class=JSONResponse)
async def model_classes(request: Request, filename: str) -> Response:
    """Return the class-name list embedded in a model file (via detector RPC)."""
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate

    target = _safe_resolve_model(filename)
    if target is None:
        return JSONResponse(
            {"ok": False, "error": "Model file not found or unsupported"},
            status_code=404,
        )

    # Web-side cache by (path, mtime, size) — see comment on _classes_cache.
    try:
        st = target.stat()
    except OSError:
        # Don't surface raw OSError text (CodeQL py/stack-trace-exposure);
        # log with a request id the operator can grep for.  Same pattern
        # config.py uses for structured-form errors.
        req_id = uuid.uuid4().hex[:8]
        logger.exception("model_classes: stat() failed [%s] for %s", req_id, filename)
        return JSONResponse(
            {"ok": False, "error": f"Unable to stat model file (request_id={req_id})"},
            status_code=500,
        )
    cache_key = (str(target), st.st_mtime_ns, st.st_size)
    cached = _classes_cache.get(cache_key)
    if cached is not None:
        return JSONResponse({**cached, "cached": True})

    try:
        result = await _fetch_model_classes_via_redis(str(target))
    except Exception:
        # Redis connection/auth failure, serialisation error, etc.  Return
        # the route's structured {ok:false,error:...} shape so the UI gets
        # a graceful error path instead of a 500.
        req_id = uuid.uuid4().hex[:8]
        logger.exception(
            "Redis RPC failed [%s] while introspecting %s", req_id, filename,
        )
        return JSONResponse({
            "ok": False,
            "error": f"Unable to reach detector for class introspection (request_id={req_id})",
        })

    if result.get("ok"):
        if len(_classes_cache) >= _CLASSES_CACHE_MAX:
            oldest = next(iter(_classes_cache))
            _classes_cache.pop(oldest, None)
        _classes_cache[cache_key] = {
            "ok": True,
            "classes": list(result.get("classes") or []),
            "warning": result.get("warning"),
            "model_path": filename,
        }
    return JSONResponse(result)
