"""Read and write scarguard.yml with simple file-level locking."""

import copy
import os
import threading
import time
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/config/scarguard.yml"))

# Dead config keys stripped on save.  Add keys here when removing deprecated
# features — no per-feature migration function needed.
_STALE_TOP_KEYS: set[str] = {"ssl"}
# Nested keys under ``notifications`` stripped on save (legacy flat format).
_STALE_NOTIFICATION_KEYS: set[str] = {"discord", "email"}

_lock = threading.Lock()
_cache_cfg: dict | None = None
_cache_mtime_ns: int | None = None
_cache_loaded_at = 0.0


def _read_unlocked() -> dict:
    try:
        with CONFIG_PATH.open() as f:
            loaded = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}

    if not isinstance(loaded, dict):
        raise ValueError("Config root must be a mapping (YAML dictionary)")
    return loaded


def load() -> dict:
    with _lock:
        return _read_unlocked()


def load_cached(ttl_seconds: float = 1.0) -> dict:
    """Load config using a short-lived in-memory cache with mtime invalidation."""
    global _cache_cfg, _cache_mtime_ns, _cache_loaded_at

    with _lock:
        now = time.monotonic()
        try:
            current_mtime_ns = CONFIG_PATH.stat().st_mtime_ns
        except FileNotFoundError:
            current_mtime_ns = None

        cache_valid = (
            _cache_cfg is not None
            and _cache_mtime_ns == current_mtime_ns
            and (now - _cache_loaded_at) < ttl_seconds
        )
        if cache_valid:
            cached_cfg = _cache_cfg
            if cached_cfg is None:
                return {}
            return copy.deepcopy(cached_cfg)

        cfg = _read_unlocked()
        _cache_cfg = cfg
        _cache_mtime_ns = current_mtime_ns
        _cache_loaded_at = now
        return copy.deepcopy(cfg)


def save(cfg: dict) -> None:
    global _cache_cfg, _cache_mtime_ns, _cache_loaded_at
    import tempfile
    for key in _STALE_TOP_KEYS:
        cfg.pop(key, None)
    notif = cfg.get("notifications")
    if isinstance(notif, dict):
        for key in _STALE_NOTIFICATION_KEYS:
            notif.pop(key, None)
    with _lock:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(CONFIG_PATH.parent), suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
            os.replace(tmp_path, str(CONFIG_PATH))
        except BaseException:
            os.unlink(tmp_path)
            raise
        _cache_cfg = copy.deepcopy(cfg)
        try:
            _cache_mtime_ns = CONFIG_PATH.stat().st_mtime_ns
        except FileNotFoundError:
            _cache_mtime_ns = None
        _cache_loaded_at = time.monotonic()


def set_armed(armed: bool) -> None:
    cfg = load()
    cfg.setdefault("system", {})["armed"] = armed
    save(cfg)
