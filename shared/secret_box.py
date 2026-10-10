"""At-rest encryption for sensitive scarguard.yml fields.

Uses :class:`cryptography.fernet.Fernet` (AES-128-CBC + HMAC-SHA256, with
authenticated encryption and rotation-friendly key IDs). The on-disk key
lives at ``/data/secret_key`` as base64 of 32 random bytes - generated
automatically on first web startup, chmod 600, never embedded in the
repo.

Encrypted values get a ``"enc:v1:"`` prefix so:

* round-trip parsing is unambiguous (callers know whether a value needs
  decryption);
* a future format bump can use ``"enc:v2:"`` without churn;
* migration from plaintext is trivial - decrypt is a no-op on
  unprefixed values, encrypt is a no-op on already-prefixed ones.

Sensitive field paths live here too so the redact and encrypt code share
one source of truth. Adding a new sensitive field means updating one
constant.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Callable, Iterator

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

DEFAULT_KEY_PATH = os.environ.get("SECRET_KEY_PATH", "/data/secret_key")
PREFIX = "enc:v1:"

# Structural paths (tuple of dict keys, ``"[]"`` = every list item) for
# sensitive fields outside the heterogeneous channels list. Matches
# config_redact._STRUCTURAL_PATHS. Every reader of these fields (web,
# detector, trainer, notifier, deterrent, off-watchdog) decrypts at load.
SENSITIVE_FIELD_PATHS: tuple[tuple[str, ...], ...] = (
    ("cameras", "[]", "rtsp_url"),
    ("deterrent", "tuya", "api_key"),
    ("deterrent", "tuya", "api_secret"),
    ("training", "sources", "roboflow", "api_key"),
)

# Per-channel sensitive keys. Channels under ``notifications.channels`` are
# heterogeneous dicts whose shape depends on ``type``; any matching key is
# encrypted regardless of type. Mirrors config_redact._CHANNEL_SENSITIVE_KEYS.
SENSITIVE_CHANNEL_KEYS: frozenset[str] = frozenset({
    "webhook_url",
    "smtp_pass",
    "auth_token",
    "token",
    "password",
})


class SecretKeyMissing(RuntimeError):
    """Raised when the secret key is unavailable or unusable."""


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def _fernet(key: bytes) -> Fernet:
    """Build a :class:`Fernet` instance from raw or base64-encoded *key*."""
    raw = _normalize_key(key)
    return Fernet(base64.urlsafe_b64encode(raw))


def _normalize_key(key: bytes) -> bytes:
    """Coerce *key* to a 32-byte raw key.

    Accepts:
    * 32 raw bytes (pass-through)
    * 44+ bytes of standard or url-safe base64 that decode to 32 bytes
    * Anything else → :class:`SecretKeyMissing`.
    """
    if isinstance(key, str):
        key = key.encode("ascii")
    key = key.strip()
    if len(key) == 32:
        return key
    for decoder_name in ("urlsafe_b64decode", "b64decode"):
        try:
            decoded: bytes = getattr(base64, decoder_name)(key)
            if len(decoded) == 32:
                return decoded
        except Exception:
            continue
    raise SecretKeyMissing(f"Key must decode to 32 bytes; got {len(key)}")


def encrypt(plaintext: str, key: bytes) -> str:
    """Encrypt *plaintext* and return ``enc:v1:<token>``.

    Idempotent: an already-encrypted value passes through. An empty string
    passes through (don't burn ciphertext on absent fields)."""
    if not isinstance(plaintext, str) or not plaintext:
        return plaintext
    if is_encrypted(plaintext):
        return plaintext
    token = _fernet(key).encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{PREFIX}{token}"


def decrypt(value: str, key: bytes) -> str:
    """Decrypt *value* if encrypted, reject unencrypted values.

    Prior to v1.15 a plaintext passthrough existed for one-time migration
    from pre-v1.14 configs. That branch has been removed - all sensitive
    fields must be encrypted before loading.
    """
    if not isinstance(value, str):
        return value
    if not is_encrypted(value):
        raise ValueError(
            f"Unencrypted sensitive value rejected (missing '{PREFIX}' prefix). "
            "Run a config save to trigger encryption, or rotate the secret key."
        )
    token = value[len(PREFIX):]
    try:
        return _fernet(key).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretKeyMissing(
            "Failed to decrypt - wrong key or corrupted ciphertext",
        ) from exc


def generate_key() -> bytes:
    """Return a fresh 32-byte raw key (base64-encoded for on-disk storage)."""
    return base64.b64encode(os.urandom(32))


def write_key_if_missing(path: str | None = None) -> bool:
    """Write a freshly-generated key to *path* if absent. Returns True if
    a key was created. Sets permissions to 0600 so a compromised non-root
    sidecar cannot read it.

    *path* defaults to :data:`DEFAULT_KEY_PATH` at *call* time (not import
    time) so tests can monkey-patch the module attribute."""
    if path is None:
        path = DEFAULT_KEY_PATH
    if os.path.exists(path):
        return False
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    key = generate_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    logger.info("Generated new secret key at %s", path)
    return True


def load_key(path: str | None = None) -> bytes:
    """Read the secret key from disk. Raises :class:`SecretKeyMissing`.

    See :func:`write_key_if_missing` re. *path* default resolution."""
    if path is None:
        path = DEFAULT_KEY_PATH
    if not os.path.exists(path):
        raise SecretKeyMissing(f"Secret key not found at {path}")
    with open(path, "rb") as f:
        raw = f.read().strip()
    if not raw:
        raise SecretKeyMissing(f"Secret key file at {path} is empty")
    # Validate by normalising - raises on bad data.
    _normalize_key(raw)
    return raw


def try_load_key(path: str | None = None) -> bytes | None:
    """Like :func:`load_key` but returns ``None`` instead of raising."""
    try:
        return load_key(path)
    except SecretKeyMissing as exc:
        logger.warning("%s - secrets will be read as plaintext", exc)
        return None


# ── Walking helpers ─────────────────────────────────────────────────────────

def _path_leaves(cfg: Any, path: tuple[str, ...]) -> Iterator[tuple[dict, str]]:
    """Yield ``(container, key)`` for every leaf at *path* (``"[]"`` fans out)."""
    if not path:
        return
    head, rest = path[0], path[1:]
    if head == "[]":
        if isinstance(cfg, list):
            for item in cfg:
                yield from _path_leaves(item, rest)
        return
    if not isinstance(cfg, dict) or head not in cfg:
        return
    if not rest:
        yield cfg, head
    else:
        yield from _path_leaves(cfg[head], rest)


def _sensitive_leaves(cfg: Any) -> Iterator[tuple[dict, str]]:
    """Yield ``(container, key)`` for every sensitive field present in *cfg*."""
    for path in SENSITIVE_FIELD_PATHS:
        yield from _path_leaves(cfg, path)
    notifications = cfg.get("notifications") if isinstance(cfg, dict) else None
    channels = notifications.get("channels") if isinstance(notifications, dict) else None
    if isinstance(channels, list):
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            for k in sorted(SENSITIVE_CHANNEL_KEYS):
                if k in channel:
                    yield channel, k


def _apply(cfg: Any, apply_fn: Callable[[str], str | None]) -> int:
    count = 0
    for container, k in _sensitive_leaves(cfg):
        v = container[k]
        if isinstance(v, str) and v:
            new_v = apply_fn(v)
            if new_v is not None:
                container[k] = new_v
                count += 1
    return count


def encrypt_in_place(cfg: dict, key: bytes) -> int:
    """Walk *cfg* and encrypt every plaintext sensitive field. Returns count
    of newly-encrypted fields. Already-encrypted values are left as-is."""
    return _apply(cfg, lambda v: None if is_encrypted(v) else encrypt(v, key))


def decrypt_in_place(cfg: dict, key: bytes) -> int:
    """Walk *cfg* and decrypt every encrypted sensitive field. Returns count
    of newly-decrypted fields. Plaintext values are left as-is."""
    return _apply(cfg, lambda v: decrypt(v, key) if is_encrypted(v) else None)


def has_plaintext_secrets(cfg: dict) -> bool:
    """True iff any sensitive field is plaintext (i.e. needs migration)."""
    return any(
        isinstance(c[k], str) and c[k] and not is_encrypted(c[k])
        for c, k in _sensitive_leaves(cfg)
    )


def has_encrypted_secrets(cfg: dict) -> bool:
    """True iff any sensitive field holds an ``enc:v1:`` value."""
    return any(is_encrypted(c[k]) for c, k in _sensitive_leaves(cfg))
