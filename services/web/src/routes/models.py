import json
import logging
import os
import re
import time as _time
import uuid
from functools import partial
from pathlib import Path
from typing import Any

import audit
import config_store
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from model_store import (
    UNRESOLVED,
    CandidateValidationError,
    ModelStore,
    ModelStoreError,
    safe_model_name,
    sha256_file,
)
from rate_limit_dep import rate_limit
from route_auth import current_user, has_admin_access, require_admin, require_viewer
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.responses import Response
from upload_limits import file_limit, upload_form

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/models"))
# Candidates, rollback copies and the promotion ledger live outside MODELS_DIR
# so nothing unpromoted is selectable as the live model (SG-07).
MODEL_STORE_DIR = Path(os.environ.get("MODEL_STORE_DIR", "/data/model_store"))
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")
# Fixed texts so a crafted link cannot display an arbitrary success message.
_NOTICES = {
    "promoted": "Candidate promoted. A copy of any file it replaced is under Rollback Copies.",
    "restored": "Rollback copy restored.",
    "discarded": "Candidate discarded.",
}
ALLOWED_EXTENSIONS = {".pt", ".engine", ".onnx"}
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024


_CLASSES_REQUEST_CHANNEL = "scarguard:model.classes.request"
_CLASSES_RESPONSE_PREFIX = "scarguard:model.classes.response:"
_CLASSES_TIMEOUT_SEC = 15.0

# Cache of detector RPC results keyed on (abs_path, mtime_ns, size).  Size is
# included to defeat in-place rewrites that preserve mtime (e.g. ``os.replace``
# from model promotion, rsync ``--times``).  Detector has its own cache too;
# this web-side cache short-circuits the pub/sub round-trip when a user
# re-opens the Models page or the Config chip picker re-renders before
# detector's cache sees the request.
_classes_cache: dict[tuple[str, int, int], dict[str, Any]] = {}
_CLASSES_CACHE_MAX = 32
_sha_cache: dict[tuple[str, int, int], str] = {}


def _store() -> ModelStore:
    return ModelStore(MODEL_STORE_DIR, MODELS_DIR)


def _actor(request: Request) -> str:
    """Username for the ledger; auth-disabled installs record "anonymous"."""
    user = current_user(request) or {}
    return str(user.get("username") or "unknown")


def _live_sha256(path: Path) -> str | None:
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _sha_cache.get(key)
    if cached is None:
        try:
            cached = sha256_file(path)
        except OSError:
            return None
        if len(_sha_cache) >= _CLASSES_CACHE_MAX:
            _sha_cache.pop(next(iter(_sha_cache)), None)
        _sha_cache[key] = cached
    return cached


def _page_context(
    is_admin: bool,
    *,
    uploaded: str = "",
    error: str | None = None,
    notice: str | None = None,
) -> dict[str, Any]:
    """Gather page data; hashes and directory scans, so run it off the event loop."""
    store: ModelStore | None
    try:
        store = _store()
    except (OSError, ModelStoreError):
        logger.exception("Model store unavailable at %s", MODEL_STORE_DIR)
        store = None
    files = _list_files()
    active_name = Path(
        str(config_store.load_cached().get("detection", {}).get("model_path", ""))
    ).name
    index = store.provenance_index() if store is not None else {}
    for entry in files:
        entry["active"] = entry["name"] == active_name
        sha256 = _live_sha256(MODELS_DIR / str(entry["name"]))
        entry["sha256"] = sha256
        entry["provenance"] = index.get((str(entry["name"]), sha256 or ""), {"status": UNRESOLVED})
    candidates = store.list_candidates() if store else []
    # Only echo a name that belongs to a real candidate, never raw URL text.
    uploaded_name = next(
        (str(c["requested_name"]) for c in candidates if c["id"] == uploaded), ""
    )
    return {
        "files": files,
        "uploaded": uploaded_name,
        "error": error if store is not None else (error or "Model candidate store is unavailable"),
        "notice": notice,
        "candidates": candidates,
        "rollbacks": store.list_rollbacks() if store else [],
        "history": store.history(limit=20) if store else [],
        "is_admin": is_admin,
    }


async def _render(request: Request, status_code: int = 200, **kwargs: Any) -> Response:
    context = await run_in_threadpool(
        partial(_page_context, has_admin_access(request), **kwargs)
    )
    return templates.TemplateResponse(request, "models.html", context, status_code=status_code)


@router.get("", response_class=HTMLResponse)
async def models_page(request: Request, uploaded: str = "", notice: str = "") -> Response:
    """Model management page. Readable by viewer + admin; changes are admin-only."""
    gate = require_viewer(request)
    if not isinstance(gate, dict):
        return gate
    return await _render(request, uploaded=uploaded, notice=_NOTICES.get(notice))


@router.post(
    "", response_class=HTMLResponse,
    dependencies=[Depends(rate_limit("model-upload", capacity=10, window_seconds=3600))],
)
async def upload_model(request: Request) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    async with upload_form(request, "/models") as form:
        file = form.get("file")
        if not isinstance(file, StarletteUploadFile):
            return JSONResponse({"error": "File required"}, status_code=422)
        return await _save_model(request, file)


def _requested_name(filename: str) -> str:
    """A safe default promotion name derived from the uploaded file name."""
    name = _UNSAFE_NAME_CHARS.sub("_", Path(filename).name).lstrip("._-")
    try:
        return safe_model_name(name)
    except ValueError:
        return f"uploaded{Path(filename).suffix.lower()}"


async def _save_model(request: Request, file: StarletteUploadFile) -> Response:
    """Stage an upload as a validated candidate. MODELS_DIR is never written (SG-07)."""
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    max_upload_bytes = file_limit("/models")
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        await file.close()
        return await _render(
            request,
            error=f"Unsupported file type '{suffix}'. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    try:
        staged = await run_in_threadpool(lambda: _store().stage(suffix))
    except (OSError, ModelStoreError):
        await file.close()
        logger.exception("Could not stage model upload in %s", MODEL_STORE_DIR)
        return await _render(request, status_code=500, error="Model candidate store is unavailable")

    committing = False
    try:
        while True:
            chunk = await file.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            if staged.size + len(chunk) > max_upload_bytes:
                await run_in_threadpool(staged.abort)
                max_size_mb = round(max_upload_bytes / 1_048_576, 1)
                audit.record_request(
                    request, action="model.candidate_rejected",
                    resource=Path(filename).name[:255],
                    details={"reason": f"exceeds {max_size_mb} MB"},
                )
                return await _render(
                    request, error=f"Upload exceeds max size of {max_size_mb} MB."
                )
            await run_in_threadpool(staged.write, chunk)
        # commit() cleans up after itself on failure; once it has started, a
        # cancelled request must not race it by deleting the staging directory
        # (a commit that completes after cancellation leaves a valid candidate
        # whose manifest still names the uploader, without a web audit row).
        committing = True
        manifest = await run_in_threadpool(
            partial(
                staged.commit,
                source="upload",
                requested_name=_requested_name(filename),
                provenance={
                    "uploaded_by": _actor(request),
                    "original_filename": Path(filename).name[:255],
                    "client_ip": request.client.host if request.client else None,
                },
            )
        )
    except CandidateValidationError as exc:
        audit.record_request(
            request, action="model.candidate_rejected", resource=Path(filename).name[:255],
            details={"reason": str(exc)[:500]},
        )
        return await _render(request, error=f"Upload rejected - {exc}")
    except (ModelStoreError, OSError):
        if not committing:
            staged.abort()
        logger.exception("Could not publish uploaded model candidate")
        return await _render(request, status_code=500, error="Could not store the upload")
    except BaseException:
        if not committing:
            staged.abort()
        raise
    finally:
        await file.close()

    audit.record_request(
        request, action="model.candidate_upload", resource=manifest["id"],
        details={"name": manifest["requested_name"], "sha256": manifest["sha256"]},
    )
    return RedirectResponse(url=f"/models?uploaded={manifest['id']}", status_code=303)


async def _form_text(request: Request, key: str) -> str:
    form = await request.form()
    value = form.get(key)
    return value.strip() if isinstance(value, str) else ""


@router.post("/candidates/{candidate_id}/promote", response_class=HTMLResponse)
async def promote_candidate(request: Request, candidate_id: str) -> Response:
    """Explicit admin promotion: rollback copy first, then atomic replace + ledger."""
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    target = await _form_text(request, "target_name")
    try:
        store = _store()
        target = target or str(store.get_candidate(candidate_id).get("requested_name", ""))
        record = await run_in_threadpool(
            partial(store.promote, candidate_id, target, actor=_actor(request))
        )
    except (ModelStoreError, OSError) as exc:
        logger.warning("Model promotion of %s failed: %s", candidate_id, exc)
        audit.record_request(
            request, action="model.promote_failed", resource=candidate_id[:64],
            details={"target_name": target[:128], "reason": str(exc)[:500]},
        )
        return await _render(request, status_code=400, error=f"Promotion failed - {exc}")
    _forget_cached(record["target_name"])
    audit.record_request(request, action="model.promote", resource=record["target_name"],
                         details=record)
    return RedirectResponse(url="/models?notice=promoted", status_code=303)


@router.post("/rollback/{rollback_id}", response_class=HTMLResponse)
async def restore_rollback(request: Request, rollback_id: str) -> Response:
    """Restore a pre-promotion copy (the current file is itself kept as a rollback)."""
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    try:
        record = await run_in_threadpool(
            partial(_store().rollback, rollback_id, actor=_actor(request))
        )
    except (ModelStoreError, OSError) as exc:
        logger.warning("Model rollback %s failed: %s", rollback_id, exc)
        audit.record_request(
            request, action="model.rollback_failed", resource=rollback_id[:64],
            details={"reason": str(exc)[:500]},
        )
        return await _render(request, status_code=400, error=f"Rollback failed - {exc}")
    _forget_cached(record["target_name"])
    audit.record_request(request, action="model.rollback", resource=record["target_name"],
                         details=record)
    return RedirectResponse(url="/models?notice=restored", status_code=303)


@router.post("/candidates/{candidate_id}/discard", response_class=HTMLResponse)
async def discard_candidate(request: Request, candidate_id: str) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate
    try:
        record = await run_in_threadpool(
            partial(_store().discard_candidate, candidate_id, actor=_actor(request))
        )
    except (ModelStoreError, OSError) as exc:
        return await _render(request, status_code=400, error=f"Discard failed - {exc}")
    audit.record_request(request, action="model.candidate_discard", resource=candidate_id,
                         details=record)
    return RedirectResponse(url="/models?notice=discarded", status_code=303)


def _forget_cached(name: str) -> None:
    # A promotion may change the embedded class names of this file name.
    dest_str = str((MODELS_DIR / name).resolve())
    for key in list(_classes_cache.keys()):
        if key[0] == dest_str:
            _classes_cache.pop(key, None)


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
    with a regex gate - the taint tracker can't follow sanitisation
    through Path joins), we enumerate the files actually present in
    ``MODELS_DIR`` and treat *filename* as a dict key against that
    known-safe set.  The returned ``Path`` therefore always comes from
    a trusted directory listing - the user-supplied string never reaches
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
                # request_id matches before trusting it - a stale publisher
                # or a shared-channel misroute could otherwise poison the
                # cache with someone else's result.
                if not isinstance(reply, dict) or reply.get("request_id") != request_id:
                    continue
                return reply
        return {
            "ok": False,
            "error": "Request timed out - detector may not be running",
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

    # Web-side cache by (path, mtime, size) - see comment on _classes_cache.
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
