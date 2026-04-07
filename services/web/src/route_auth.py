"""Shared route-level authorisation helpers.

Replaces the ad-hoc ``_require_admin`` / ``_is_admin`` helpers that were
previously duplicated across route files.  Defines the three role tiers
(user / viewer / admin, see auth.py for the contract) and exposes:

Predicates (return bool, no side effects):
    - is_authenticated(request)
    - has_user_access(request)   — any authenticated role
    - has_viewer_access(request) — viewer or admin
    - has_admin_access(request)  — admin only
    - current_user(request)      — the user dict, or None
    - current_role(request)      — 'user' / 'viewer' / 'admin' / ''

Guards (return the user dict on success, or a Response to return from the
route handler — the caller checks ``isinstance(result, dict)``):
    - require_user(request, is_api=False)
    - require_viewer(request, is_api=False)
    - require_admin(request, is_api=False)

Usage pattern for guarded routes::

    @router.post("/config")
    async def save_config(request: Request) -> Response:
        user_or_resp = require_admin(request)
        if not isinstance(user_or_resp, dict):
            return user_or_resp
        user = user_or_resp
        # ... do the thing
"""

from __future__ import annotations

from typing import Any

from auth import ROLE_ADMIN, ROLE_USER, ROLE_VIEWER, VALID_ROLES
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

# Re-export for route files that need the constants without importing auth.py.
__all__ = [
    "ROLE_USER",
    "ROLE_VIEWER",
    "ROLE_ADMIN",
    "VALID_ROLES",
    "current_user",
    "current_role",
    "is_authenticated",
    "has_user_access",
    "has_viewer_access",
    "has_admin_access",
    "require_user",
    "require_viewer",
    "require_admin",
]


# ── Predicates ──────────────────────────────────────────────────────────────


def current_user(request: Request) -> dict[str, Any] | None:
    """Return the authenticated user dict from request.state, or None.

    The auth middleware at services/web/src/main.py populates
    request.state.user on every request.  This helper normalises the
    "maybe unset attribute" case.
    """
    user = getattr(request.state, "user", None)
    if isinstance(user, dict):
        return user
    return None


def current_role(request: Request) -> str:
    """Return the role of the current user as a lowercase string.

    Returns ``""`` if not authenticated.  Prefers the new ``role`` column;
    falls back to the legacy ``is_admin`` boolean for DBs that haven't been
    migrated yet (pre-v0.12.7) or for sessions issued before the migration
    ran.
    """
    user = current_user(request)
    if not user:
        return ""
    role = user.get("role")
    if isinstance(role, str) and role in VALID_ROLES:
        return role
    # Legacy fallback: is_admin=1 → admin, else user.  Viewers did not exist
    # before v0.12.7 so anyone without an explicit role cannot be one.
    return ROLE_ADMIN if user.get("is_admin") else ROLE_USER


def is_authenticated(request: Request) -> bool:
    return current_user(request) is not None


def has_user_access(request: Request) -> bool:
    """True if the caller is any authenticated user (user/viewer/admin)."""
    return current_role(request) in VALID_ROLES


def has_viewer_access(request: Request) -> bool:
    """True if the caller is a viewer or admin (i.e. allowed to view admin pages)."""
    return current_role(request) in (ROLE_VIEWER, ROLE_ADMIN)


def has_admin_access(request: Request) -> bool:
    """True if the caller is an admin (full write + secret access)."""
    return current_role(request) == ROLE_ADMIN


# ── Guards ──────────────────────────────────────────────────────────────────


def _deny(is_api: bool, message: str) -> Response:
    """Build the appropriate denial response for API vs HTML routes."""
    if is_api:
        return JSONResponse({"error": message}, status_code=403)
    # HTML routes historically redirect to '/' on insufficient privilege;
    # preserve that behaviour so the templates don't need to change.
    return RedirectResponse("/", status_code=302)


def require_user(
    request: Request,
    *,
    is_api: bool = False,
) -> dict[str, Any] | Response:
    """Return the user dict if authenticated, else a denial Response.

    Middleware should have already rejected unauthenticated requests, so
    this is primarily a type-safe way for route handlers to get the user
    dict without repeating the ``getattr(..., None)`` dance.
    """
    user = current_user(request)
    if user is None:
        return _deny(is_api, "authentication required")
    return user


def require_viewer(
    request: Request,
    *,
    is_api: bool = False,
) -> dict[str, Any] | Response:
    """Return the user dict if viewer or admin, else a denial Response."""
    user = current_user(request)
    if user is None or current_role(request) not in (ROLE_VIEWER, ROLE_ADMIN):
        return _deny(is_api, "viewer or admin role required")
    return user


def require_admin(
    request: Request,
    *,
    is_api: bool = False,
) -> dict[str, Any] | Response:
    """Return the user dict if admin, else a denial Response."""
    user = current_user(request)
    if user is None or current_role(request) != ROLE_ADMIN:
        return _deny(is_api, "admin role required")
    return user
