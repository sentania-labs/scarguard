"""Read and write scarguard.yml with simple file-level locking."""

import copy
import logging
import os
import threading
import time
from pathlib import Path

import secret_box
import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/config/scarguard.yml"))

# Dead config keys stripped on save.  Add keys here when removing deprecated
# features — no per-feature migration function needed.
_STALE_TOP_KEYS: set[str] = {"ssl"}
# Nested keys under ``notifications`` stripped on save (legacy flat format).
_STALE_NOTIFICATION_KEYS: set[str] = {"discord", "email"}
# Per-camera keys stripped on save.  action_rules was renamed to
# notification_rules in v0.13.3 and is migrated on read.
_STALE_CAMERA_KEYS: set[str] = {"action_rules"}
# Nested keys under ``system`` stripped on save.
# v1.14: snapshot_retention_days and metrics_retention_days were consolidated
# into a single retention_days in v0.11. The startup migration in main.py
# still handles in-memory consolidation; this strips raw-YAML residue and
# any pre-v0.11 configs that haven't been touched in years.
_STALE_SYSTEM_KEYS: set[str] = {"snapshot_retention_days", "metrics_retention_days"}


def _migrate_in_place(cfg: dict) -> None:
    """Apply one-shot renames to *cfg* so older on-disk configs round-trip
    through the v0.13.3 Pydantic models without losing data.

    - Per-camera ``action_rules`` → ``notification_rules`` (v0.13.3 rename).
    """
    cameras = cfg.get("cameras")
    if not isinstance(cameras, list):
        return
    migrated = 0
    for cam in cameras:
        if not isinstance(cam, dict):
            continue
        if "action_rules" in cam and "notification_rules" not in cam:
            cam["notification_rules"] = cam.pop("action_rules")
            migrated += 1
        elif "action_rules" in cam:
            # both present → notification_rules wins; legacy action_rules dropped
            cam.pop("action_rules")
            migrated += 1
    if migrated:
        logger.info(
            "Migrated %d camera(s) from action_rules → notification_rules", migrated,
        )

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
    _migrate_in_place(loaded)
    _decrypt_secrets_in_place(loaded)
    return loaded


def _decrypt_secrets_in_place(cfg: dict) -> None:
    """Decrypt sensitive fields if a secret key is available.

    Plaintext values pass through unchanged (migration mode). When the key
    is absent (e.g. /data not yet writable, or a fresh upgrade) we leave
    encrypted values as-is — the consumer will get the ``enc:v1:`` prefix
    and surface a configuration error rather than a silently-broken
    integration.
    """
    key = secret_box.try_load_key()
    if key is None:
        return
    try:
        secret_box.decrypt_in_place(cfg, key)
    except secret_box.SecretKeyMissing:
        logger.error(
            "Failed to decrypt one or more secrets — wrong key on disk?",
        )


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
    # Migrate first so legacy keys become new keys (preserves their data);
    # THEN strip any still-present stale keys (raw YAML edits only).
    _migrate_in_place(cfg)
    for key in _STALE_TOP_KEYS:
        cfg.pop(key, None)
    notif = cfg.get("notifications")
    if isinstance(notif, dict):
        for key in _STALE_NOTIFICATION_KEYS:
            notif.pop(key, None)
    sys = cfg.get("system")
    if isinstance(sys, dict):
        for key in _STALE_SYSTEM_KEYS:
            sys.pop(key, None)
    cameras = cfg.get("cameras")
    if isinstance(cameras, list):
        for cam in cameras:
            if not isinstance(cam, dict):
                continue
            for key in _STALE_CAMERA_KEYS:
                cam.pop(key, None)
    # Encrypt sensitive fields before writing to disk. Idempotent — values
    # already in ``enc:v1:`` form pass through. If no key is available we
    # write plaintext (migration mode), and a one-shot startup migration in
    # main.py will re-save once the key is generated.
    sk = secret_box.try_load_key()
    if sk is not None:
        encrypted = secret_box.encrypt_in_place(cfg, sk)
        if encrypted:
            logger.info("Encrypted %d sensitive field(s) before save", encrypted)
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


def set_deterrent_enabled(enabled: bool) -> None:
    cfg = load()
    # Tolerate a non-mapping `deterrent:` in YAML (e.g. `false`, null, or
    # a stray scalar) — coerce to a fresh dict before subscripting,
    # matching how _deterrent_context() defensively reads it.
    if not isinstance(cfg.get("deterrent"), dict):
        cfg["deterrent"] = {}
    cfg["deterrent"]["enabled"] = enabled
    save(cfg)
