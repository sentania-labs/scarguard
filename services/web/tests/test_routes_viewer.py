"""Integration tests for the v0.12.7 viewer role across web routes.

Verifies that:
- admin can access every protected route
- viewer can access the read-only subset and is blocked on writes
- regular user is blocked on admin-only reads and on admin writes
- unauthenticated requests are rejected

Uses a fixture that enables auth and monkeypatches ``auth_module.validate_session``
to return a user dict based on the session cookie value ("admin" / "viewer" /
"user" / missing).  This avoids standing up a real login flow while still
exercising the real ``route_auth`` helpers and middleware code path.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

# Sample config with a few secrets in it — so we can assert they're masked
# for the viewer.
_SECRET_CFG: dict[str, Any] = {
    "system": {"armed": True, "log_level": "info", "auth": {"enabled": True}},
    "cameras": [
        {
            "name": "pond-north",
            "rtsp_url": "rtsps://172.16.0.1:7441/VIEWER_SECRET_123?enableSrtp",
            "enabled": True,
            "resolution": 720,
        }
    ],
    "detection": {
        "model_path": "/models/best.pt",
        "confidence_threshold": 0.25,
        "target_classes": ["bird"],
        "cooldown_seconds": 30,
        "frame_skip": 2,
    },
    "redis": {"host": "localhost", "port": 6379},
    "notifications": {
        "discord": {
            "enabled": True,
            "webhook_url": "https://discord.com/api/webhooks/111/VIEWER_DISCORD_SECRET",
        },
        "email": {
            "enabled": True,
            "smtp_host": "smtp.example.com",
            "smtp_user": "ops@example.com",
            "smtp_pass": "VIEWER_EMAIL_SECRET",
        },
        "channels": [],
    },
    "tls": {"mode": "off", "domain": ""},
}

# Map from session cookie value to the user dict the (patched) middleware
# should return.  None → unauthenticated.
_ROLE_USERS: dict[str, dict[str, Any] | None] = {
    "admin-session": {
        "user_id": 1,
        "username": "scott",
        "is_admin": 1,
        "role": "admin",
        "disabled": 0,
    },
    "viewer-session": {
        "user_id": 2,
        "username": "bob",
        "is_admin": 0,
        "role": "viewer",
        "disabled": 0,
    },
    "user-session": {
        "user_id": 3,
        "username": "alice",
        "is_admin": 0,
        "role": "user",
        "disabled": 0,
    },
}


@pytest.fixture
def auth_client(monkeypatch):
    """TestClient with auth enabled and validate_session patched per-cookie."""
    cfg = copy.deepcopy(_SECRET_CFG)

    monkeypatch.setattr("config_store.load", lambda: cfg)
    monkeypatch.setattr("config_store.load_cached", lambda **_kw: cfg)
    monkeypatch.setattr("config_store.save", lambda _cfg: None)
    monkeypatch.setattr("config_store.set_armed", lambda _armed: None)
    monkeypatch.setattr("db.get_latest_event", lambda: None)
    monkeypatch.setattr("db.count_events", lambda **_kw: 0)
    monkeypatch.setattr("db.get_events", lambda **_kw: [])
    monkeypatch.setattr("db.get_latest_snapshots_by_camera", lambda: {})
    monkeypatch.setattr("db.get_feedback_stats", lambda **_kw: {"total": 0, "by_class": {}})
    monkeypatch.setattr("db.count_exportable_events", lambda **_kw: 0)

    # Patch validate_session so the middleware resolves the fake cookies in
    # _ROLE_USERS into their corresponding user dicts.  API token validation
    # is left alone — those tests use the cookie path.
    import auth as auth_module

    def _fake_validate_session(_db, raw_token):
        return _ROLE_USERS.get(raw_token)

    monkeypatch.setattr(auth_module, "validate_session", _fake_validate_session)
    # Also make users_exist return True so the first-run setup redirect doesn't fire
    monkeypatch.setattr(auth_module, "users_exist", lambda _p: True)
    # Avoid touching the real auth.db for user-management routes.  We only
    # care that the request reaches the handler with the right role — the
    # handler's SQL path is exercised by test_auth_roles.py unit tests.
    monkeypatch.setattr(auth_module, "list_users", lambda _db: [])
    monkeypatch.setattr(auth_module, "list_api_tokens", lambda _db, user_id=None: [])

    from fastapi.testclient import TestClient
    from main import app

    c = TestClient(app)
    # Default to sending Accept: text/html so the auth middleware's HTML-path
    # (302 redirect to /login) is exercised for anonymous tests instead of
    # the JSON-path (401).  Individual API tests can override the header.
    c.headers["Accept"] = "text/html"
    # Prime CSRF cookie via an anonymous GET that will redirect to /login.
    c.get("/login", follow_redirects=False)
    csrf_token = c.cookies.get("csrf_token", "")
    c.headers["X-CSRF-Token"] = csrf_token
    return c


def _as(client, role: str):
    """Return a context manager that sets the session cookie for the given role."""
    cookie_map = {
        "admin": "admin-session",
        "viewer": "viewer-session",
        "user": "user-session",
    }
    token = cookie_map.get(role)
    if token:
        client.cookies.set("session", token)
    else:
        client.cookies.pop("session", None)
    return client


# ── Anonymous / unauthenticated ───────────────────────────────────────────────

class TestAnonymous:
    def test_dashboard_redirects_to_login(self, auth_client):
        _as(auth_client, "")
        r = auth_client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers.get("location", "")

    def test_config_redirects_to_login(self, auth_client):
        _as(auth_client, "")
        r = auth_client.get("/config", follow_redirects=False)
        assert r.status_code == 302


# ── Regular "user" role ───────────────────────────────────────────────────────

class TestUserRole:
    def test_user_can_see_dashboard(self, auth_client):
        _as(auth_client, "user")
        assert auth_client.get("/").status_code == 200

    def test_user_cannot_see_config(self, auth_client):
        _as(auth_client, "user")
        r = auth_client.get("/config", follow_redirects=False)
        # require_viewer denies: redirects to /
        assert r.status_code == 302
        assert r.headers.get("location") == "/"

    def test_user_cannot_post_config(self, auth_client):
        _as(auth_client, "user")
        r = auth_client.post(
            "/config/structured",
            json={"system": {}, "cameras": [], "detection": {}, "notifications": {}, "tls": {}},
        )
        # Admin-only API endpoint → JSON 403
        assert r.status_code == 403

    def test_user_cannot_access_raw_config(self, auth_client):
        _as(auth_client, "user")
        r = auth_client.get("/config/raw")
        assert r.status_code == 403

    def test_user_cannot_see_users_page(self, auth_client):
        _as(auth_client, "user")
        r = auth_client.get("/admin/users", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers.get("location") == "/"

    def test_user_cannot_see_training(self, auth_client):
        _as(auth_client, "user")
        r = auth_client.get("/admin/training", follow_redirects=False)
        assert r.status_code == 302


# ── Viewer (read-only admin) ──────────────────────────────────────────────────

class TestViewerRole:
    def test_viewer_dashboard(self, auth_client):
        _as(auth_client, "viewer")
        assert auth_client.get("/").status_code == 200

    def test_viewer_config_page_ok(self, auth_client):
        _as(auth_client, "viewer")
        r = auth_client.get("/config")
        assert r.status_code == 200

    def test_viewer_config_page_masks_secrets(self, auth_client):
        _as(auth_client, "viewer")
        r = auth_client.get("/config")
        body = r.text
        # Plaintext secrets from _SECRET_CFG must not appear anywhere on the page
        assert "VIEWER_SECRET_123" not in body
        assert "VIEWER_DISCORD_SECRET" not in body
        assert "VIEWER_EMAIL_SECRET" not in body
        # The read-only banner is present
        assert "Read-only view" in body
        # The placeholder does appear (at least once)
        assert "***REDACTED***" in body

    def test_viewer_config_page_keeps_camera_entries(self, auth_client):
        """Regression for the PR #94 review — feeding a redacted dict into
        `_parse_cfg` caused `CameraConfig.rtsp_url` validation to fail on
        the `***REDACTED***` placeholder, dropping the camera from the
        form entirely.  The camera *name* must still reach the rendered
        page (in the cameras_json hydration payload) so the viewer can
        see that the camera exists, just with the URL masked.
        """
        _as(auth_client, "viewer")
        body = auth_client.get("/config").text
        assert "pond-north" in body
        assert "VIEWER_SECRET_123" not in body
        assert "***REDACTED***" in body

    def test_viewer_config_raw_denied(self, auth_client):
        _as(auth_client, "viewer")
        r = auth_client.get("/config/raw")
        assert r.status_code == 403

    def test_viewer_post_config_denied(self, auth_client):
        _as(auth_client, "viewer")
        r = auth_client.post(
            "/config/structured",
            json={"system": {}, "cameras": [], "detection": {}, "notifications": {}, "tls": {}},
        )
        assert r.status_code == 403

    def test_viewer_can_see_training(self, auth_client):
        _as(auth_client, "viewer")
        r = auth_client.get("/admin/training")
        assert r.status_code == 200

    def test_viewer_cannot_post_training_promote(self, auth_client):
        _as(auth_client, "viewer")
        r = auth_client.post(
            "/admin/training/promote",
            data={"model_path": "best.pt"},
            follow_redirects=False,
        )
        # HTML route → redirect to /
        assert r.status_code == 302
        assert r.headers.get("location") == "/"

    def test_viewer_cannot_see_users_page(self, auth_client):
        _as(auth_client, "viewer")
        r = auth_client.get("/admin/users", follow_redirects=False)
        assert r.status_code == 302

    def test_viewer_can_see_logs(self, auth_client):
        _as(auth_client, "viewer")
        r = auth_client.get("/admin/logs")
        assert r.status_code == 200

    def test_viewer_can_see_backups_list(self, auth_client):
        _as(auth_client, "viewer")
        # backup_manager is None in the test app startup, so the route
        # falls back to an empty list — still returns 200.
        r = auth_client.get("/admin/backups")
        assert r.status_code == 200

    def test_viewer_can_see_models_list(self, auth_client):
        _as(auth_client, "viewer")
        r = auth_client.get("/models")
        assert r.status_code == 200

    def test_viewer_cannot_upload_model(self, auth_client):
        _as(auth_client, "viewer")
        # Posting an empty multipart upload still triggers the auth check
        # before FastAPI's validation of the UploadFile field.
        r = auth_client.post(
            "/models",
            files={"file": ("fake.pt", b"\x00\x00", "application/octet-stream")},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers.get("location") == "/"


# ── Admin ─────────────────────────────────────────────────────────────────────

class TestAdminRole:
    def test_admin_sees_everything(self, auth_client):
        _as(auth_client, "admin")
        for path in (
            "/",
            "/config",
            "/admin/training",
            "/admin/logs",
            "/admin/backups",
            "/admin/users",
        ):
            r = auth_client.get(path)
            assert r.status_code == 200, f"{path} returned {r.status_code} for admin"

    def test_admin_config_raw_ok(self, auth_client):
        _as(auth_client, "admin")
        r = auth_client.get("/config/raw")
        assert r.status_code == 200

    def test_admin_config_page_shows_plaintext_secrets(self, auth_client):
        _as(auth_client, "admin")
        body = auth_client.get("/config").text
        # Admin sees the real values (verifies the read_only branch is gated
        # correctly on the role, not accidentally applied to everyone).
        assert "VIEWER_SECRET_123" in body
        assert "Read-only view" not in body
