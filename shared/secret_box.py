"""At-rest encryption for sensitive scarguard.yml fields.

Uses :class:`cryptography.fernet.Fernet` (AES-128-CBC + HMAC-SHA256, with
authenticated encryption and rotation-friendly key IDs). The on-disk key
lives at ``/data/secret_key`` as base64 of 32 random bytes — generated
automatically on first web startup, chmod 600, never embedded in the
repo.

Encrypted values get a ``"enc:v1:"`` prefix so:

* round-trip parsing is unambiguous (callers know whether a value needs
  decryption);
* a future format bump can use ``"enc:v2:"`` without churn;
* migration from plaintext is trivial — decrypt is a no-op on
  unprefixed values, encrypt is a no-op on already-prefixed ones.

Sensitive field paths live here too so the redact and encrypt code share
one source of truth. Adding a new sensitive field means updating one
constant.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Callable

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

DEFAULT_KEY_PATH = os.environ.get("SECRET_KEY_PATH", "/data/secret_key")
PREFIX = "enc:v1:"

# Structural paths (tuple of dict keys) for sensitive fields outside the
# heterogeneous channels list. Format matches config_redact._STRUCTURAL_PATHS
# but excludes ``cameras[].rtsp_url`` — encrypting that requires teaching
# the detector container to decrypt at boot, which is deferred.
SENSITIVE_FIELD_PATHS: tuple[tuple[str, ...], ...] = (
    ("deterrent", "tuya", "api_key"),
    ("deterrent", "tuya", "api_secret"),
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
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            decoded: bytes = decoder(key)  # type: ignore[operator]
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
    """Decrypt *value* if encrypted, otherwise pass through.

    Plaintext passthrough exists for migration: a deployment upgrading
    from a pre-v1.14 release will have plaintext values until the first
    save. v1.15 will remove this branch and reject unencrypted values.
    """
    if not isinstance(value, str):
        return value
    if not is_encrypted(value):
        return value
    token = value[len(PREFIX):]
    try:
        return _fernet(key).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretKeyMissing(
            "Failed to decrypt — wrong key or corrupted ciphertext",
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
    # Validate by normalising — raises on bad data.
    _normalize_key(raw)
    return raw


def try_load_key(path: str | None = None) -> bytes | None:
    """Like :func:`load_key` but returns ``None`` instead of raising."""
    try:
        return load_key(path)
    except SecretKeyMissing as exc:
        logger.warning("%s — secrets will be read as plaintext", exc)
        return None


# ── Walking helpers ─────────────────────────────────────────────────────────

def _safe_get(cfg: Any, path: tuple[str, ...]) -> Any:
    cur = cfg
    for seg in path:
        if not isinstance(cur, dict) or seg not in cur:
            return None
        cur = cur[seg]
    return cur


def _walk_and_apply(
    cfg: Any,
    path: tuple[str, ...],
    apply_fn: Callable[[str], str],
) -> bool:
    """Apply *apply_fn* to the leaf at *path* in *cfg*. Returns True if applied."""
    if not path or not isinstance(cfg, dict):
        return False
    head = path[0]
    if head not in cfg:
        return False
    if len(path) == 1:
        v = cfg[head]
        if isinstance(v, str) and v:
            cfg[head] = apply_fn(v)
            return True
        return False
    return _walk_and_apply(cfg[head], path[1:], apply_fn)


def encrypt_in_place(cfg: dict, key: bytes) -> int:
    """Walk *cfg* and encrypt every plaintext sensitive field. Returns count
    of newly-encrypted fields. Already-encrypted values are left as-is."""
    count = 0

    for path in SENSITIVE_FIELD_PATHS:
        leaf = _safe_get(cfg, path)
        if isinstance(leaf, str) and leaf and not is_encrypted(leaf):
            if _walk_and_apply(cfg, path, lambda v: encrypt(v, key)):
                count += 1

    channels = _safe_get(cfg, ("notifications", "channels"))
    if isinstance(channels, list):
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            for k in SENSITIVE_CHANNEL_KEYS:
                v = channel.get(k)
                if isinstance(v, str) and v and not is_encrypted(v):
                    channel[k] = encrypt(v, key)
                    count += 1

    return count


def decrypt_in_place(cfg: dict, key: bytes) -> int:
    """Walk *cfg* and decrypt every encrypted sensitive field. Returns count
    of newly-decrypted fields. Plaintext values are left as-is."""
    count = 0

    for path in SENSITIVE_FIELD_PATHS:
        leaf = _safe_get(cfg, path)
        if is_encrypted(leaf):
            _walk_and_apply(cfg, path, lambda v: decrypt(v, key))
            count += 1

    channels = _safe_get(cfg, ("notifications", "channels"))
    if isinstance(channels, list):
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            for k in SENSITIVE_CHANNEL_KEYS:
                v = channel.get(k)
                if isinstance(v, str) and is_encrypted(v):
                    channel[k] = decrypt(v, key)
                    count += 1

    return count


def has_plaintext_secrets(cfg: dict) -> bool:
    """True iff any sensitive field is plaintext (i.e. needs migration)."""
    for path in SENSITIVE_FIELD_PATHS:
        v = _safe_get(cfg, path)
        if isinstance(v, str) and v and not is_encrypted(v):
            return True
    channels = _safe_get(cfg, ("notifications", "channels"))
    if isinstance(channels, list):
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            for k in SENSITIVE_CHANNEL_KEYS:
                v = channel.get(k)
                if isinstance(v, str) and v and not is_encrypted(v):
                    return True
    return False
