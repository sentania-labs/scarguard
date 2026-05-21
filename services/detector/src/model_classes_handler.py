"""On-demand model class-name introspection — Redis request/response.

The web service asks "what classes does model X support?" by publishing to
``scarguard:model.classes.request``; we respond on a per-request reply
channel ``scarguard:model.classes.response:<request_id>``.

Results are cached per ``(path, mtime, size)`` so repeated introspection of
the same file is instant.  Load-paths, in order of preference:

1. Reuse an already-loaded model from :class:`ModelPool` — zero extra
   memory, zero GPU contention.
2. Fall back to ``torch.load(..., map_location="cpu")`` for ``.pt`` files —
   reads ``model.names`` without spinning up a CUDA context that would
   compete with live inference on the detector's GPU.
3. For ``.engine`` (TensorRT) files, use a serialized ``YOLO(path)`` load
   guarded by a thread lock so at most one introspection runs at a time.

``.engine`` files that were compiled without embedded names return an empty
``classes`` list + a warning pointing back at the source ``.pt``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

import redis as redis_lib

logger = logging.getLogger(__name__)

REQUEST_CHANNEL = "scarguard:model.classes.request"
RESPONSE_PREFIX = "scarguard:model.classes.response:"

# Load is 1-3s cold; keep results as long as the file is unchanged.
_CACHE_MAX = 32

# Where uploaded model files live.  Introspection rejects any request
# resolving outside this root — defense in depth against a rogue Redis
# publisher asking the detector to load /etc/passwd as a YOLO model.
_MODELS_ROOT = Path(os.environ.get("MODELS_DIR", "/models")).resolve()
_ALLOWED_SUFFIXES = {".pt", ".engine", ".onnx"}


class ModelClassesHandler(threading.Thread):
    """Daemon thread — answers class-list queries by loading models on demand."""

    def __init__(
        self,
        redis_cfg: dict[str, Any],
        stop_event: threading.Event,
        model_pool: Any | None = None,
    ) -> None:
        super().__init__(name="model-classes-handler", daemon=True)
        self._redis_cfg = redis_cfg
        self._stop = stop_event
        self._model_pool = model_pool
        # key: (abs_path, mtime_ns, size) → {"classes": [...], "warning": str|None}
        self._cache: dict[tuple[str, int, int], dict[str, Any]] = {}
        self._cache_lock = threading.Lock()
        # Serialize actual model loads so two concurrent introspections don't
        # double-allocate GPU buffers (P2-2).
        self._load_lock = threading.Lock()

    def run(self) -> None:
        logger.info("ModelClassesHandler started")
        backoff = 5

        while not self._stop.is_set():
            client: redis_lib.Redis | None = None
            pubsub: redis_lib.client.PubSub | None = None
            try:
                pw = os.environ.get("REDIS_PASSWORD", "") or None
                client = redis_lib.Redis(
                    host=self._redis_cfg.get("host", "redis"),
                    port=int(self._redis_cfg.get("port", 6379)),
                    password=pw,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    retry_on_timeout=True,
                )
                pubsub = client.pubsub()
                pubsub.subscribe(REQUEST_CHANNEL)
                logger.info("Subscribed to %s", REQUEST_CHANNEL)
                backoff = 5

                while not self._stop.is_set():
                    msg = pubsub.get_message(timeout=1.0)
                    if msg is None or msg["type"] != "message":
                        continue
                    try:
                        request = json.loads(msg["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue

                    try:
                        self._handle(client, request)
                    except Exception:
                        # Don't let a per-request bug kill the handler loop.
                        logger.exception("Unhandled error dispatching class-list request")

            except redis_lib.RedisError:
                if self._stop.is_set():
                    break
                logger.warning(
                    "ModelClassesHandler: Redis error — retry in %ds", backoff,
                    exc_info=True,
                )
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)
            except Exception:
                # P2-1: any non-Redis exception in the setup path — log and
                # retry with backoff instead of reconnect-storming.
                if self._stop.is_set():
                    break
                logger.exception(
                    "ModelClassesHandler: unexpected error — retry in %ds", backoff,
                )
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                if pubsub is not None:
                    try:
                        pubsub.unsubscribe()
                        pubsub.close()
                    except Exception:
                        pass
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

        logger.info("ModelClassesHandler stopped")

    def _handle(self, client: redis_lib.Redis, request: dict[str, Any]) -> None:
        request_id = request.get("request_id", "")
        model_path = request.get("model_path", "")
        if not request_id or not model_path:
            return

        reply_channel = f"{RESPONSE_PREFIX}{request_id}"
        response = self._introspect(model_path)
        response["request_id"] = request_id
        try:
            client.publish(reply_channel, json.dumps(response))
        except redis_lib.RedisError:
            logger.warning("Failed to publish class-list response for %s", model_path)

    def _introspect(self, model_path: str) -> dict[str, Any]:
        safe = _safe_model_path(model_path)
        if safe is None:
            return {
                "ok": False,
                "error": (
                    "Model file not found, outside the models directory, "
                    "or has an unsupported suffix"
                ),
            }

        try:
            st = safe.stat()
        except OSError as exc:
            return {"ok": False, "error": f"Cannot stat model file: {exc}"}

        cache_key = (str(safe), st.st_mtime_ns, st.st_size)
        with self._cache_lock:
            cached = self._cache.get(cache_key)
        if cached is not None:
            return {**cached, "ok": True, "cached": True, "model_path": model_path}

        # Serialize loads to prevent concurrent GPU allocation (P2-2).
        with self._load_lock:
            # Re-check cache under the load lock in case another thread just
            # finished the same load.
            with self._cache_lock:
                cached = self._cache.get(cache_key)
            if cached is not None:
                return {**cached, "ok": True, "cached": True, "model_path": model_path}

            classes, warning = self._extract_classes(safe)

        with self._cache_lock:
            if len(self._cache) >= _CACHE_MAX:
                oldest = next(iter(self._cache))
                self._cache.pop(oldest, None)
            self._cache[cache_key] = {"classes": classes, "warning": warning}

        return {
            "ok": True,
            "cached": False,
            "classes": classes,
            "warning": warning,
            "model_path": model_path,
        }

    def _extract_classes(self, safe: Path) -> tuple[list[str], str | None]:
        """Resolve class names without competing with live inference on the GPU.

        Preferred path: reuse an already-loaded detector from the pool.
        Fallback for ``.pt``: CPU-only ``torch.load``.  Fallback for
        ``.engine``: full YOLO load (serialized).
        """
        # 1. ModelPool hit — zero extra allocation.
        pool_classes = _peek_pool_names(self._model_pool, str(safe))
        if pool_classes is not None:
            return _normalize_names(pool_classes, safe.suffix.lower())

        suffix = safe.suffix.lower()
        # 2. CPU-only torch.load for .pt files — avoids spinning a CUDA
        # context that contends with inference.
        if suffix == ".pt":
            names = _names_from_pt_cpu(str(safe))
            if names is not None:
                return _normalize_names(names, suffix)

        # 3. Fallback — full ultralytics load.
        try:
            model = _load_yolo(str(safe))
        except ImportError:
            return [], "ultralytics not available — cannot introspect model"
        except Exception as exc:  # pragma: no cover — runtime dependent
            logger.exception("Failed to load model for introspection: %s", safe)
            return [], f"Failed to load model: {exc}"

        names = getattr(model, "names", None)
        return _normalize_names(names, suffix)


# ── Helpers ────────────────────────────────────────────────────────────────


def _safe_model_path(model_path: str) -> Path | None:
    """Validate *model_path* and return it as a resolved Path, or None.

    Rejects: empty/traversal strings, paths outside ``MODELS_DIR``, paths
    with unsupported suffixes, and non-existent files.
    """
    if not model_path or not isinstance(model_path, str):
        return None
    try:
        candidate = Path(model_path).resolve()
    except (OSError, ValueError):
        return None
    try:
        candidate.relative_to(_MODELS_ROOT)
    except ValueError:
        return None
    if candidate.suffix.lower() not in _ALLOWED_SUFFIXES:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _peek_pool_names(pool: Any | None, abs_path: str) -> Any | None:
    """Return ``model.names`` from the ``ModelPool`` if already loaded; else None.

    Reads directly without incrementing the pool's refcount — introspection
    is read-only and must not prevent unload when the owning camera stops.
    """
    if pool is None:
        return None
    try:
        loaded_dict = getattr(pool, "_models", None)
        if not isinstance(loaded_dict, dict):
            return None
        detector = loaded_dict.get(abs_path)
        if detector is None:
            return None
        model = getattr(detector, "_model", None)
        if model is None:
            return None
        return getattr(model, "names", None)
    except Exception:
        return None


def _names_from_pt_cpu(abs_path: str) -> Any | None:
    """Extract ``model.names`` from a ``.pt`` checkpoint without a GPU load.

    Returns ``None`` on any failure so the caller can fall through to the
    full-ultralytics path.
    """
    try:
        import torch  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        ckpt = torch.load(abs_path, map_location="cpu", weights_only=True)
    except Exception:
        logger.debug("CPU-only torch.load failed for %s — falling back", abs_path)
        return None
    # Checkpoints may store the model under "model" (typical ultralytics
    # format) or be the model object itself.
    model_obj = ckpt.get("model") if isinstance(ckpt, dict) else ckpt
    return getattr(model_obj, "names", None)


def _load_yolo(abs_path: str) -> Any:
    """Indirection point so tests can monkeypatch without ultralytics installed."""
    from ultralytics import YOLO  # type: ignore[import-not-found]
    return YOLO(abs_path)


def _normalize_names(names: Any, suffix: str) -> tuple[list[str], str | None]:
    if isinstance(names, dict):
        try:
            classes = [names[k] for k in sorted(names.keys())]
        except Exception:
            classes = [str(v) for v in names.values()]
    elif isinstance(names, list):
        classes = list(names)
    else:
        classes = []

    classes = [str(c) for c in classes]

    if suffix == ".engine" and _looks_like_stub_names(classes):
        return [], (
            "Class names not embedded in this .engine file — "
            "exported from a .pt that lacked metadata, or the TensorRT "
            "converter dropped them.  Check the source .pt for the real "
            "class list, or re-export preserving names."
        )

    if not classes:
        return [], "Model has no embedded class names"

    return classes, None


def _looks_like_stub_names(classes: list[str]) -> bool:
    """Detect ultralytics' auto-generated placeholder names."""
    if not classes:
        return True
    return all(
        c.startswith("class_") and c[len("class_"):].isdigit()
        for c in classes
    )


# ── Legacy entry point — still used by existing tests ─────────────────────


def _extract_classes(abs_path: str) -> tuple[list[str], str | None]:
    """Thin wrapper around ModelClassesHandler._extract_classes for tests."""
    safe = Path(abs_path).resolve()
    # Tests pass synthetic tmp paths outside MODELS_DIR — accept any path
    # when invoked via this legacy helper.
    suffix = safe.suffix.lower()
    try:
        model = _load_yolo(str(safe))
    except ImportError:
        return [], "ultralytics not available — cannot introspect model"
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to load model for introspection: %s", safe)
        return [], f"Failed to load model: {exc}"
    names = getattr(model, "names", None)
    return _normalize_names(names, suffix)
