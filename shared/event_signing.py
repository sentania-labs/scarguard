"""HMAC-SHA256 signing for Redis pub/sub detection events.

The deterrent service fires physical devices (sprinklers, sirens) based on
detection messages arriving over Redis pub/sub. Anything inside the
internal Docker network — a compromised service, a future sidecar, a
misconfigured container — can publish a fake detection and cause physical
actuation. Redis password auth gates Redis access itself; it does not
authenticate individual publishers on the same bus.

This module signs every published detection with an HMAC derived from a
shared key (``DETECTION_HMAC_KEY``) generated once per deployment by
``setup.sh`` and distributed via ``.env``. Subscribers verify the
signature before treating the event as authoritative.

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

logger = logging.getLogger(__name__)

SIGNATURE_FIELD = "_sig"
ENV_VAR = "DETECTION_HMAC_KEY"


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


def sign_event(payload: dict, key: bytes) -> dict:
    """Return a copy of *payload* with an HMAC signature attached.

    The signature covers the canonical JSON of every field except
    ``_sig`` itself, keyed by *key* under HMAC-SHA256. The returned dict
    is safe to JSON-serialise and publish.
    """
    sig = hmac.new(key, _canonical_payload(payload), hashlib.sha256).hexdigest()
    return {**payload, SIGNATURE_FIELD: sig}


def verify_event(event: dict, key: bytes) -> bool:
    """Return True iff *event* carries a valid signature under *key*.

    Uses :func:`hmac.compare_digest` so timing attacks can't distinguish
    "no signature" from "wrong signature" from "right signature". Malformed
    events (missing ``_sig``, non-hex, wrong length) return False rather
    than raising.
    """
    sig = event.get(SIGNATURE_FIELD)
    if not isinstance(sig, str):
        return False
    try:
        expected = hmac.new(key, _canonical_payload(event), hashlib.sha256).hexdigest()
    except Exception:
        return False
    return hmac.compare_digest(sig, expected)


def load_key_from_env(var_name: str = ENV_VAR) -> bytes | None:
    """Read and decode the HMAC key from the environment.

    The key is stored in ``.env`` as a base64-encoded 32-byte secret. A
    missing or empty value returns ``None`` — callers should log a
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
            "%s is set but not valid base64 — treating as absent",
            var_name,
        )
        return None
    if len(key) < 16:
        logger.error(
            "%s decodes to %d bytes (need >= 16) — treating as absent",
            var_name, len(key),
        )
        return None
    return key
