"""Sensitive-field redaction for read-only admin ("viewer") role.

The `viewer` role can view the config editor form but must not see any
plaintext secrets — RTSP URLs (contain auth tokens), Discord webhook URLs
(entirely sensitive), SMTP passwords, webhook auth tokens, ntfy tokens and
basic-auth passwords, and any custom auth headers.

This module centralises the redaction rules so they live in one place
rather than being scattered across templates / routes / JSON serialisers.
Call `redact_config(cfg)` before passing config dicts to templates or API
responses for viewer-role users.  Call `redact_yaml(yaml_text)` for the
raw-YAML code paths (backup diffs, etc.).
"""

from __future__ import annotations

import copy
from typing import Any

import yaml

REDACTED_PLACEHOLDER = "***REDACTED***"

# Structural paths — these are masked by walking the config tree explicitly.
# Handles the known Pydantic model shapes from config_model.py.
_STRUCTURAL_PATHS: tuple[tuple[str, ...], ...] = (
    ("cameras", "[]", "rtsp_url"),
    ("deterrent", "tuya", "api_key"),
    ("deterrent", "tuya", "api_secret"),
)

# Field-name heuristic for the heterogeneous `notifications.channels` list
# (each channel is a dict whose shape depends on `type`: discord, email,
# webhook, ntfy, etc).  Any key matching one of these in a channel dict is
# masked, regardless of channel type.  Keep this in sync with the channel
# builders in services/web/src/static/config.js and the notifier modules.
_CHANNEL_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "webhook_url",   # discord channels
    "smtp_pass",     # email channels
    "auth_token",    # webhook channels (bearer token)
    "token",         # ntfy channels (bearer)
    "password",      # ntfy channels (basic auth)
})

# Substring match for custom HTTP header names that probably carry an auth
# credential.  Used by the ``headers`` dict walker below so non-sensitive
# routing headers (Content-Type, X-Source, etc.) stay visible while
# Authorization / X-API-Key / Cookie / Token headers get masked.
_HEADER_SENSITIVE_PATTERNS: tuple[str, ...] = (
    "auth",
    "token",
    "key",
    "secret",
    "cookie",
    "password",
)


def redact_config(
    cfg: dict[str, Any],
    *,
    placeholder: str = REDACTED_PLACEHOLDER,
) -> dict[str, Any]:
    """Return a deep copy of *cfg* with sensitive fields masked.

    Preserves field *presence* (so a viewer can see *that* a Discord webhook
    is configured for channel X, just not its URL).  Handles missing
    optional sections without raising — if ``cfg`` has no ``notifications``
    key, nothing in that subtree is touched.

    Idempotent: redacting an already-redacted config is a no-op.
    """
    if not isinstance(cfg, dict):
        return cfg
    out = copy.deepcopy(cfg)

    # Walk each structural path and replace the leaf value if it exists and
    # is truthy.  Empty strings stay empty (so the form still shows "no
    # Discord webhook configured" rather than "***REDACTED***" for an unset
    # field).
    for path in _STRUCTURAL_PATHS:
        _mask_path(out, path, placeholder)

    # Walk notifications.channels (a list of heterogeneous dicts) and mask
    # any key matching the sensitive-keys heuristic.  ``headers`` is special-
    # cased: it's a sub-dict whose keys are HTTP header names — walk it and
    # mask values whose header name looks like a credential, preserving the
    # structural type (still a dict) so the form's JS hydration doesn't
    # break.
    channels = _safe_get(out, ("notifications", "channels"))
    if isinstance(channels, list):
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            for key in _CHANNEL_SENSITIVE_KEYS:
                if key in channel and channel[key]:
                    channel[key] = placeholder
            hdrs = channel.get("headers")
            if isinstance(hdrs, dict) and hdrs:
                channel["headers"] = _mask_header_values(hdrs, placeholder)

    return out


def _mask_header_values(
    headers: dict[str, Any],
    placeholder: str,
) -> dict[str, Any]:
    """Return a copy of *headers* with credential-looking values masked.

    A header value is masked when its key (case-insensitive) contains any of
    the ``_HEADER_SENSITIVE_PATTERNS`` substrings.  This keeps routing /
    diagnostic headers (Content-Type, X-Source, Accept) visible while
    ensuring Authorization, X-API-Key, Cookie, etc. are hidden.
    """
    out: dict[str, Any] = {}
    for name, value in headers.items():
        key = str(name).lower()
        if any(p in key for p in _HEADER_SENSITIVE_PATTERNS) and value:
            out[name] = placeholder
        else:
            out[name] = value
    return out


def redact_yaml(
    yaml_text: str,
    *,
    placeholder: str = REDACTED_PLACEHOLDER,
) -> str:
    """Round-trip *yaml_text* through `redact_config`.

    Used by the backup-diff code path where we have raw YAML strings rather
    than parsed dicts.  Preserves valid YAML output; comments and ordering
    are NOT preserved (PyYAML limitation).  For the diff viewer that's
    acceptable — the reader still sees structure and knows which fields
    exist, just not the plaintext secret values.
    """
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        # If the input isn't valid YAML, return it unchanged.  The caller
        # shouldn't be handing us malformed YAML but we'd rather render a
        # broken diff than crash the route.
        return yaml_text
    if not isinstance(parsed, dict):
        return yaml_text
    redacted = redact_config(parsed, placeholder=placeholder)
    return yaml.safe_dump(redacted, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Internal walkers
# ---------------------------------------------------------------------------


def _mask_path(
    obj: dict[str, Any],
    path: tuple[str, ...],
    placeholder: str,
) -> None:
    """Walk *path* into *obj* (mutating) and replace the leaf with placeholder.

    Path segments are dict keys, with the special token ``"[]"`` meaning
    "iterate every element of the list at this position".  Missing keys are
    silently skipped — this is a best-effort masker, not a validator.
    """
    if not path:
        return
    head, *rest = path

    if head == "[]":
        # `obj` should be a list at this point; the caller should have
        # recursed into it.  Defensive no-op if not.
        return

    if head not in obj:
        return
    value = obj[head]

    if not rest:
        # Leaf: mask if truthy (preserve empty/unset fields as-is).
        if value:
            obj[head] = placeholder
        return

    # Intermediate node.  If the next segment is "[]", iterate the list.
    next_seg, *tail = rest
    if next_seg == "[]":
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _mask_path(item, tuple(tail), placeholder)
        return

    if isinstance(value, dict):
        _mask_path(value, tuple(rest), placeholder)


def _safe_get(obj: Any, path: tuple[str, ...]) -> Any:
    """Return the value at *path* in *obj*, or None if any step is missing."""
    cur: Any = obj
    for seg in path:
        if not isinstance(cur, dict) or seg not in cur:
            return None
        cur = cur[seg]
    return cur
