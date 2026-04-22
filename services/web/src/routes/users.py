"""User and API token management routes (admin only).

In v0.12.7 the binary ``is_admin`` flag was replaced with a three-value
``role`` enum (user / viewer / admin).  User management is still gated to
admins only — viewers do not see this page at all.

Lockout protection: the "last admin" cannot be demoted, disabled, or
deleted.  A SELECT COUNT guard in auth.count_active_admins() enforces
this so a misclick can't orphan the instance.
"""

from __future__ import annotations

import os
import sqlite3
from urllib.parse import quote

import audit
import auth as auth_module
from auth import ROLE_ADMIN, VALID_ROLES
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from route_auth import current_role, require_admin

_src = os.path.dirname(os.path.dirname(__file__))
templates = Jinja2Templates(directory=os.path.join(_src, "templates"))

router = APIRouter(prefix="/admin/users")

AUTH_DB_PATH = os.environ.get("AUTH_DB_PATH", "/data/auth.db")


def _redirect_err(msg: str) -> RedirectResponse:
    """Build a redirect back to the users list with an error query param."""
    return RedirectResponse(f"/admin/users?error={quote(msg)}", status_code=302)


def _actor(request: Request) -> tuple[int | None, str | None, str | None]:
    """Return (user_id, username, client_ip) for the authenticated caller."""
    user = getattr(request.state, "user", None)
    uid = user.get("user_id") if user else None
    uname = user.get("username") if user else None
    ip = request.client.host if request.client else None
    return uid, uname, ip


# ── User list ─────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def users_list(request: Request) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate

    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        users = auth_module.list_users(db)
        tokens = auth_module.list_api_tokens(db)
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "users": users,
            "tokens": tokens,
            "new_token": None,
            "error": request.query_params.get("error"),
            "valid_roles": sorted(VALID_ROLES),
        },
    )


# ── Create user ───────────────────────────────────────────────────────────────

@router.post("")
async def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
) -> RedirectResponse:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        # require_admin returned a RedirectResponse — it happens to be the
        # right type here, but cast for mypy.
        return gate  # type: ignore[return-value]

    if role not in VALID_ROLES:
        return _redirect_err(f"Invalid role: {role}")
    from routes.auth import MIN_PASSWORD_LEN, _is_common_password
    if len(password) < MIN_PASSWORD_LEN:
        return _redirect_err(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    if _is_common_password(password):
        return _redirect_err("That password is too common — please pick a less predictable one.")

    uid, uname, ip = _actor(request)
    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        new_id = auth_module.create_user(db, username.strip(), password, role=role)
        audit.record(
            db,
            action="user.create",
            user_id=uid,
            username=uname,
            client_ip=ip,
            resource=f"user:{new_id}",
            details={"new_username": username.strip(), "role": role},
        )
    except sqlite3.IntegrityError:
        return _redirect_err(f"Username '{username}' already exists.")
    finally:
        db.close()

    return RedirectResponse("/admin/users", status_code=302)


# ── Change role ───────────────────────────────────────────────────────────────

@router.post("/{user_id}/role")
async def change_role(
    request: Request,
    user_id: int,
    role: str = Form(...),
) -> RedirectResponse:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate  # type: ignore[return-value]
    current_user = gate

    if role not in VALID_ROLES:
        return _redirect_err(f"Invalid role: {role}")

    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        target = auth_module.get_user_by_id(db, user_id)
        if target is None:
            return _redirect_err("User not found.")

        # Cannot demote yourself (defence in depth — the UI also blocks this)
        if target["id"] == current_user["user_id"] and role != ROLE_ADMIN:
            return _redirect_err("Cannot change your own role.")

        # Last-admin protection is handled atomically inside try_demote_admin:
        # the UPDATE ... WHERE ... COUNT > 1 guard prevents a race where two
        # concurrent demote requests would both observe count=2 and both
        # succeed.
        ok = auth_module.try_demote_admin(db, user_id, role)
        if not ok:
            return _redirect_err(
                "Cannot demote the last admin — promote another user first."
            )
        uid, uname, ip = _actor(request)
        audit.record(
            db,
            action="user.role_change",
            user_id=uid,
            username=uname,
            client_ip=ip,
            resource=f"user:{user_id}",
            details={"target": target["username"], "new_role": role},
        )
    finally:
        db.close()

    return RedirectResponse("/admin/users", status_code=302)


# ── Disable / enable user ─────────────────────────────────────────────────────

@router.post("/{user_id}/disable")
async def toggle_disable(request: Request, user_id: int) -> RedirectResponse:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate  # type: ignore[return-value]
    current_user = gate

    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        target = auth_module.get_user_by_id(db, user_id)
        if target is None:
            return _redirect_err("User not found.")
        if target["id"] == current_user["user_id"]:
            return _redirect_err("Cannot disable your own account.")

        new_disabled = not bool(target["disabled"])
        # Atomic last-admin guard — see try_disable_admin docstring.
        ok = auth_module.try_disable_admin(db, user_id, new_disabled)
        if not ok:
            return _redirect_err(
                "Cannot disable the last active admin — promote another user first."
            )
        uid, uname, ip = _actor(request)
        audit.record(
            db,
            action="user.disable" if new_disabled else "user.enable",
            user_id=uid,
            username=uname,
            client_ip=ip,
            resource=f"user:{user_id}",
            details={"target": target["username"]},
        )
    finally:
        db.close()

    return RedirectResponse("/admin/users", status_code=302)


# ── Change password ───────────────────────────────────────────────────────────

@router.post("/{user_id}/password")
async def change_password(
    request: Request,
    user_id: int,
    new_password: str = Form(...),
) -> RedirectResponse:
    # Admins can change anyone's password; non-admins may only change their own.
    cur = getattr(request.state, "user", None)
    if cur is None:
        return RedirectResponse("/", status_code=302)
    if current_role(request) != ROLE_ADMIN and cur.get("user_id") != user_id:
        return RedirectResponse("/", status_code=302)

    from routes.auth import MIN_PASSWORD_LEN, _is_common_password
    if len(new_password) < MIN_PASSWORD_LEN:
        return _redirect_err(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    if _is_common_password(new_password):
        return _redirect_err("That password is too common — please pick a less predictable one.")

    uid, uname, ip = _actor(request)
    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        changed = auth_module.set_user_password(db, user_id, new_password)
        if not changed:
            return _redirect_err("User not found.")
        audit.record(
            db,
            action="user.password_reset",
            user_id=uid,
            username=uname,
            client_ip=ip,
            resource=f"user:{user_id}",
            details={"self": uid == user_id},
        )
    finally:
        db.close()

    return RedirectResponse("/admin/users", status_code=302)


# ── Delete user ───────────────────────────────────────────────────────────────

@router.post("/{user_id}/delete")
async def delete_user(request: Request, user_id: int) -> RedirectResponse:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate  # type: ignore[return-value]
    current_user = gate

    if current_user["user_id"] == user_id:
        return _redirect_err("Cannot delete your own account.")

    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        target = auth_module.get_user_by_id(db, user_id)
        if target is None:
            return _redirect_err("User not found.")
        # Atomic last-admin guard — see try_delete_admin docstring.
        ok = auth_module.try_delete_admin(db, user_id)
        if not ok:
            return _redirect_err(
                "Cannot delete the last active admin — promote another user first."
            )
        uid, uname, ip = _actor(request)
        audit.record(
            db,
            action="user.delete",
            user_id=uid,
            username=uname,
            client_ip=ip,
            resource=f"user:{user_id}",
            details={"target": target["username"]},
        )
    finally:
        db.close()

    return RedirectResponse("/admin/users", status_code=302)


# ── API Tokens ────────────────────────────────────────────────────────────────

@router.post("/api-tokens", response_class=HTMLResponse)
async def create_api_token(
    request: Request,
    name: str = Form(...),
) -> Response:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate

    current_user = gate
    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        raw_token = auth_module.create_api_token(db, current_user["user_id"], name.strip())
        users = auth_module.list_users(db)
        tokens = auth_module.list_api_tokens(db)
        _, uname, ip = _actor(request)
        audit.record(
            db,
            action="api_token.create",
            user_id=current_user["user_id"],
            username=uname,
            client_ip=ip,
            details={"name": name.strip()},
        )
    finally:
        db.close()

    # Render the page directly (not a redirect) so the raw token is never in a URL,
    # browser history, or server access logs.
    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "users": users,
            "tokens": tokens,
            "new_token": raw_token,
            "error": None,
            "valid_roles": sorted(VALID_ROLES),
        },
    )


@router.post("/api-tokens/{token_id}/revoke")
async def revoke_api_token(request: Request, token_id: int) -> RedirectResponse:
    gate = require_admin(request)
    if not isinstance(gate, dict):
        return gate  # type: ignore[return-value]

    uid, uname, ip = _actor(request)
    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        if not auth_module.revoke_api_token(db, token_id):
            return _redirect_err("API token not found.")
        audit.record(
            db,
            action="api_token.revoke",
            user_id=uid,
            username=uname,
            client_ip=ip,
            resource=f"api_token:{token_id}",
        )
    finally:
        db.close()

    return RedirectResponse("/admin/users", status_code=302)
