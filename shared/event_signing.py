"""HMAC-SHA256 signing for Redis pub/sub detection events and privileged commands.

The deterrent service fires physical devices (sprinklers, sirens) based on
detection messages arriving over Redis pub/sub. Anything inside the
internal Docker network - a compromised service, a future sidecar, a
misconfigured container - can publish a fake detection and cause physical
actuation. Redis password auth gates Redis access itself; it does not
authenticate individual publishers on the same bus.

This module signs every published event with an HMAC derived from a
shared key (``DETECTION_HMAC_KEY``) generated once per deployment by
``setup.sh`` and distributed via ``.env``. Subscribers verify the
signature before treating the event as authoritative.

Channel-bound signing: the canonical form includes the target channel
string, so a signature forged on one channel cannot be replayed on
another.

Bounded replay cache: a per-service nonce set prevents the same
request_id from being accepted twice within the configured window.

Backwards compatibility: if the key is absent (older deployments mid-
upgrade), services log a loud warning and accept unsigned events. Once
operators have run the upgrade procedure, the key is set and unsigned
events are rejected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid

logger = logging.getLogger(__name__)

SIGNATURE_FIELD = "_sig"
TIMESTAMP_FIELD = "_ts"
CHANNEL_FIELD = "_ch"
NONCE_FIELD = "_nonce"
ENV_VAR = "DETECTION_HMAC_KEY"

# ── Replay cache ──────────────────────────────────────────────────────────────


class _ReplayCache:
    """Bounded set of seen nonces with automatic eviction.

    Keys are ``{channel}:{nonce}``.  Entries are evicted when the
    cache is full (FIFO via insertion order) so memory cannot grow
    unbounded on high-throughput channels.
    """

    def __init__(self, capacity: int = 4096, ttl_seconds: int = 60) -> None:
        self._capacity = capacity
        self._ttl = ttl_seconds
        # Ordered insertion: list of (channel_nonce, timestamp)
        self._seen: list[tuple[str, float]] = []
        # Fast lookup: nonce_string -> True
        self._lookup: dict[str, bool] = {}

    def is_unique(self, channel: str, nonce: str, now: float) -> bool:
        """Return True if *nonce* has not been seen for *channel* within
        the TTL window.  Prunes expired entries on every call."""
        key = channel + ":" + nonce
        # Prune expired entries from the head
        self._prune(now)
        if key in self._lookup:
            return False
        if len(self._lookup) >= self._capacity:
            # Evict oldest entry
            old_key, _ = self._seen.pop(0)
            self._lookup.pop(old_key, None)
        self._seen.append((key, now))
        self._lookup[key] = True
        return True

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        while self._seen and self._seen[0][1] < cutoff:
            self._lookup.pop(self._seen.pop(0)[0], None)


# A module-level default cache.  Services can override or create their own.
_DEFAULT_CACHE: _ReplayCache | None = None


def _get_cache() -> _ReplayCache:
    global _DEFAULT_CACHE
    if _DEFAULT_CACHE is None:
        _DEFAULT_CACHE = _ReplayCache()
    return _DEFAULT_CACHE


def set_replay_cache(capacity: int = 4096, ttl_seconds: int = 60) -> None:
    """Replace the module-level replay cache with a fresh one."""
    global _DEFAULT_CACHE
    _DEFAULT_CACHE = _ReplayCache(capacity, ttl_seconds)


# ── Envelope fields ──────────────────────────────────────────────────────────

# Maximum age of a signed message (seconds). Messages older than this are
# rejected as stale.  30 seconds is enough for internal network delay but
# tight enough to thwart delayed replay.
MESSAGE_TTL_SECONDS: int = 30


def _canonical_payload(payload: dict) -> bytes:
    """Return the deterministic byte-string covered by the HMAC.

    Excludes ``_sig`` (the signature itself), sorts keys, uses compact
    separators. Both publisher and subscriber must agree on the canonical
    form byte-for-byte, so any change here is a wire-format break.
    """
    without_sig = {k: v for k, v in payload.items() if k != SIGNATURE_FIELD}
    return json.dumps(
        without_sig,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _make_envelope(
    payload: dict,
    key: bytes,
    channel: str,
    nonce: str | None = None,
) -> dict:
    """Return a copy of *payload* with _ch, _ts, _nonce, and _sig attached.

    The signature covers {channel}.{canonical(payload)}.
    """
    now = time.time()
    if nonce is None:
        nonce = uuid.uuid4().hex[:16]
    envelope = {
        **payload,
        CHANNEL_FIELD: channel,
        TIMESTAMP_FIELD: now,
        NONCE_FIELD: nonce,
    }
    sig_input = f"{channel}." + _canonical_payload(envelope).decode("ascii")
    sig = hmac.new(key, sig_input.encode("utf-8"), hashlib.sha256).hexdigest()
    envelope[SIGNATURE_FIELD] = sig
    return envelope


def sign_event(payload: dict, key: bytes, channel: str = "") -> dict:
    """Return a copy of *payload* with a channel-bound HMAC signature.

    *channel* identifies the Redis pub/sub channel this event is intended
    for.  The signature covers both the canonical JSON and the channel
    string, so a forged event cannot be replayed on a different channel.

    When *channel* is empty the behaviour matches the original
    ``sign_event``: no channel binding, just a signature over the payload.
    """
    if channel:
        return _make_envelope(payload, key, channel)
    # Legacy path: no channel binding.
    sig = hmac.new(key, _canonical_payload(payload), hashlib.sha256).hexdigest()
    return {**payload, SIGNATURE_FIELD: sig}


def verify_event(
    event: dict,
    key: bytes,
    channel: str = "",
    cache: _ReplayCache | None = None,
) -> bool:
    """Return True iff *event* carries a valid, fresh, unique signature.

    Checks in order:

    1. Signature presence and correct key (timing-safe).
    2. Channel binding: if *channel* is provided and the event carries
       a ``_ch`` field, the fields must match exactly.
    3. Timestamp freshness: the ``_ts`` field must be within
       ``MESSAGE_TTL_SECONDS`` of the current wall clock.
    4. Uniqueness: if *cache* is provided and the event carries a
       ``_nonce`` field, the nonce must not have been seen before
       for this channel.

    The cache is only mutated *after* the HMAC compare_digest succeeds,
    so an attacker cannot populate the cache with forged nonces to
    evict a captured valid nonce.

    Malformed events (missing ``_sig``, non-hex, wrong length) return
    False rather than raising.
    """
    sig = event.get(SIGNATURE_FIELD)
    if not isinstance(sig, str):
        return False
    expected_sig_input: str | None = None
    try:
        if channel:
            # Channel-bound envelope: signature covers {channel}.canonical_json
            _ch = event.get(CHANNEL_FIELD)
            if isinstance(_ch, str) and _ch != channel:
                # Channel mismatch: the event was signed for a different
                # channel and cannot be used here.
                logger.debug(
                    "Channel mismatch: event signed for %r, expected %r",
                    _ch,
                    channel,
                )
                return False
            _ts = event.get(TIMESTAMP_FIELD)
            if isinstance(_ts, (int, float)):
                age = time.time() - _ts
                if age < 0 or age > MESSAGE_TTL_SECONDS:
                    logger.debug(
                        "Stale event: age=%.1fs (max %ds)",
                        age,
                        MESSAGE_TTL_SECONDS,
                    )
                    return False
        _canonical = _canonical_payload(event)
        if channel:
            expected_sig_input = f"{channel}." + _canonical.decode("ascii")
        else:
            # Legacy: channel empty means no channel binding.
            expected_sig_input = _canonical.decode("ascii")
        expected = hmac.new(
            key,
            expected_sig_input.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    except Exception:
        return False
    if not hmac.compare_digest(sig, expected):
        return False
    # HMAC verified successfully — *now* mutate the replay cache.
    # An internal attacker who captures a valid envelope cannot
    # populate the cache with forged nonces to evict this one.
    if channel and cache is not None:
        _nonce = event.get(NONCE_FIELD)
        if isinstance(_nonce, str):
            if not cache.is_unique(channel, _nonce, time.time()):
                logger.debug("Duplicate nonce %s on channel %s", _nonce, channel)
                return False
    return True


def derive_channel_key(base_key: bytes, channel: str) -> bytes:
    """Return a deterministic, channel-scoped key derived from *base_key*.

    Using ``HMAC(base_key, channel)`` produces a per-channel sub-key that
    cannot be forged on another channel even if an attacker knows the base
    key.  This provides isolation without requiring a separate environment
    variable per channel.
    """
    return hmac.new(base_key, channel.encode("utf-8"), hashlib.sha256).digest()


def load_key_from_env(var_name: str = "DETECTION_HMAC_KEY") -> bytes | None:
    """Read and decode the HMAC key from the environment.

    The key is stored in ``.env`` as a base64-encoded 32-byte secret. A
    missing or empty value returns ``None`` - callers should log a
    deprecation warning and fall back to accepting unsigned events for
    one release cycle.
    """
    raw = os.environ.get(var_name, "").strip()
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception:
        logger.error(
            "%s is set but not valid base64 - treating as absent",
            var_name,
        )
        return None
    if len(key) < 16:
        logger.error(
            "%s decodes to %d bytes (need >= 16) - treating as absent",
            var_name,
            len(key),
        )
        return None
    return key
