"""User and API token management routes (admin only)."""

from __future__ import annotations

import os
import sqlite3

import auth as auth_module
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

_src = os.path.dirname(os.path.dirname(__file__))
templates = Jinja2Templates(directory=os.path.join(_src, "templates"))

router = APIRouter(prefix="/admin/users")

AUTH_DB_PATH = os.environ.get("AUTH_DB_PATH", "/data/auth.db")


def _require_admin(request: Request) -> dict | None:
    """Return the current user if admin, else None."""
    user = getattr(request.state, "user", None)
    if user is None or not user.get("is_admin"):
        return None
    return user


# ── User list ─────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def users_list(request: Request) -> Response:
    if _require_admin(request) is None:
        return RedirectResponse("/", status_code=302)

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
        },
    )


# ── Create user ───────────────────────────────────────────────────────────────

@router.post("")
async def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    is_admin: str = Form("off"),
) -> RedirectResponse:
    if _require_admin(request) is None:
        return RedirectResponse("/", status_code=302)

    if len(password) < 8:
        return RedirectResponse(
            "/admin/users?error=Password+must+be+at+least+8+characters.", status_code=302
        )

    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        auth_module.create_user(db, username.strip(), password, is_admin=(is_admin == "on"))
    except sqlite3.IntegrityError:
        return RedirectResponse(
            f"/admin/users?error=Username+%27{username}%27+already+exists.", status_code=302
        )
    finally:
        db.close()

    return RedirectResponse("/admin/users", status_code=302)


# ── Disable / enable user ─────────────────────────────────────────────────────

@router.post("/{user_id}/disable")
async def toggle_disable(request: Request, user_id: int) -> RedirectResponse:
    current_user = _require_admin(request)
    if current_user is None:
        return RedirectResponse("/", status_code=302)

    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        target = auth_module.get_user_by_id(db, user_id)
        if target is None:
            return RedirectResponse("/admin/users?error=User+not+found.", status_code=302)
        if target["id"] == current_user["user_id"]:
            return RedirectResponse(
                "/admin/users?error=Cannot+disable+your+own+account.", status_code=302
            )
        auth_module.set_user_disabled(db, user_id, not bool(target["disabled"]))
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
    current_user = _require_admin(request)
    if current_user is None:
        # Non-admins may only change their own password
        cur = getattr(request.state, "user", None)
        if cur is None or cur.get("user_id") != user_id:
            return RedirectResponse("/", status_code=302)

    if len(new_password) < 8:
        return RedirectResponse(
            "/admin/users?error=Password+must+be+at+least+8+characters.", status_code=302
        )

    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        auth_module.set_user_password(db, user_id, new_password)
    finally:
        db.close()

    return RedirectResponse("/admin/users", status_code=302)


# ── Delete user ───────────────────────────────────────────────────────────────

@router.post("/{user_id}/delete")
async def delete_user(request: Request, user_id: int) -> RedirectResponse:
    current_user = _require_admin(request)
    if current_user is None:
        return RedirectResponse("/", status_code=302)

    if current_user["user_id"] == user_id:
        return RedirectResponse(
            "/admin/users?error=Cannot+delete+your+own+account.", status_code=302
        )

    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        auth_module.delete_user(db, user_id)
    finally:
        db.close()

    return RedirectResponse("/admin/users", status_code=302)


# ── API Tokens ────────────────────────────────────────────────────────────────

@router.post("/api-tokens", response_class=HTMLResponse)
async def create_api_token(
    request: Request,
    name: str = Form(...),
) -> Response:
    if _require_admin(request) is None:
        return RedirectResponse("/", status_code=302)

    current_user = request.state.user
    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        raw_token = auth_module.create_api_token(db, current_user["user_id"], name.strip())
        users = auth_module.list_users(db)
        tokens = auth_module.list_api_tokens(db)
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
        },
    )


@router.post("/api-tokens/{token_id}/revoke")
async def revoke_api_token(request: Request, token_id: int) -> RedirectResponse:
    if _require_admin(request) is None:
        return RedirectResponse("/", status_code=302)

    db = auth_module.get_db(AUTH_DB_PATH)
    try:
        auth_module.revoke_api_token(db, token_id)
    finally:
        db.close()

    return RedirectResponse("/admin/users", status_code=302)
