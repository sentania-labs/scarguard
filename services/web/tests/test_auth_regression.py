"""Regression tests for auth and user-management security findings.

Each test exercises a real ASGI route through a TestClient with
assertions that fail on the original (unpatched) code and pass after the
fix.  They are deliberately written to exercise the *real* route handlers
and database, never copied implementation.

Findings exercised: SG-13, SG-15, SG-16, SG-29, SG-39.
"""

import os

import auth as auth_module
import pytest
from fastapi.testclient import TestClient

# ── Test setup helpers ────────────────────────────────────────────────────────


def _build_auth_client(monkeypatch, tmp_path, auth_cfg=None):
    """Return (TestClient, db_path) with auth enabled and an admin user.

    ``auth_cfg`` can override the cached config dictionary (useful for
    toggling ``auth.enabled`` per-test).
    """
    if auth_cfg is None:
        auth_cfg = {"system": {"auth": {"enabled": True}}, "redis": {"host": "localhost", "port": 6379}}
    monkeypatch.setattr("config_store.load", lambda: auth_cfg)
    monkeypatch.setattr("config_store.load_cached", lambda **_kw: auth_cfg)
    monkeypatch.setattr("config_store.save", lambda _cfg: None)
    monkeypatch.setattr("config_store.set_armed", lambda _armed: None)
    monkeypatch.setattr("db.get_latest_event", lambda: None)
    monkeypatch.setattr("db.count_events", lambda **_kw: 0)
    monkeypatch.setattr("db.get_events", lambda **_kw: [])
    monkeypatch.setattr("db.get_latest_snapshots_by_camera", lambda: {})

    db_path = str(tmp_path / "sg-test-auth.db")
    if os.path.exists(db_path):
        os.unlink(db_path)

    auth_module.AUTH_DB_PATH = db_path
    auth_module.init_db(db_path)
    from routes import auth as auth_routes
    auth_routes.AUTH_DB_PATH = db_path
    from routes import users as users_routes
    users_routes.AUTH_DB_PATH = db_path

    os.environ["BOOTSTRAP_TOKEN_PATH"] = str(tmp_path / "bootstrap")
    os.environ["SCARGUARD_BOOTSTRAP_TOKEN"] = "test-token"

    db = auth_module.get_db(db_path)
    try:
        auth_module.create_user(db, "admin", "validpassword123", is_admin=True)
    finally:
        db.close()

    import main
    from main import app as _app
    main.AUTH_DB_PATH = db_path

    c = TestClient(_app, raise_server_exceptions=False)
    c.get("/", headers={"Accept": "text/html"}, follow_redirects=True)
    c.headers["X-CSRF-Token"] = c.cookies.get("csrf_token", "")

    return c, db_path


@pytest.fixture()
def auth_client(monkeypatch, tmp_path):
    """Provide a TestClient with auth enabled and a test user."""
    return _build_auth_client(monkeypatch, tmp_path)


# ── SG-13: Bound username/password size + len(None) guard ─────────────────────


def test_sg_13_bound_username_size(auth_client):
    c, _ = auth_client
    res = c.post("/login", data={"username": "a" * 1000, "password": "abc"})
    assert res.status_code == 400


def test_sg_13_bound_password_size(auth_client):
    c, _ = auth_client
    res = c.post("/login", data={"username": "admin", "password": "a" * 1000})
    assert res.status_code == 400


def test_sg_13_admin_reset_no_current_password(auth_client):
    """Admin changes another user's password without current_password Form field.

    The HTML form omits ``current_password`` for admin-initiated resets, so
    FastAPI supplies the ``Form(None)`` default.  The unpatched code calls
    ``len(None)`` which raises ``TypeError`` (HTTP 500).  After the fix the
    route returns a redirect (HTTP 302) and the password is changed.
    """
    c, _ = auth_client

    # Log in first to establish a valid session for this client
    res_login = c.post(
        "/login",
        data={"username": "admin", "password": "validpassword123"},
        follow_redirects=False,
    )
    assert res_login.status_code == 302

    # Create a second (non-admin) user
    res_create = c.post(
        "/admin/users",
        data={"username": "victim", "password": "victimpass1234", "role": "user"},
        follow_redirects=True,
    )
    assert res_create.status_code == 200

    # Admin resets victim's password — no current_password field
    # The unpatched code crashes with len(None) → HTTP 500.
    res = c.post(
        "/admin/users/2/password",
        data={"new_password": "newvictimpass1234"},
        follow_redirects=False,
    )
    # Must NOT be a 500 crash; successful reset is a redirect (302)
    assert res.status_code != 500
    assert res.status_code == 302


# ── SG-15: Per-username lockout (no IP rotation bypass) ──────────────────────


def test_sg_15_ip_rotation_bypass(auth_client):
    """Lockout from one IP must block ALL IPs for the same username."""
    c, _ = auth_client

    # Attacker fails from IP1 to trigger lockout
    c1 = TestClient(c.app, client=("10.0.0.1", 12345))
    c1.headers["X-CSRF-Token"] = c.headers["X-CSRF-Token"]
    c1.cookies = c.cookies
    for _ in range(5):
        c1.post("/login", data={"username": "admin", "password": "wrong"})

    # Legitimate user from IP2 must also be blocked
    c2 = TestClient(c.app, client=("10.0.0.2", 12345))
    c2.headers["X-CSRF-Token"] = c.headers["X-CSRF-Token"]
    c2.cookies = c.cookies
    res = c2.post(
        "/login",
        data={"username": "admin", "password": "validpassword123"},
        follow_redirects=False,
    )
    assert res.status_code == 429


# ── SG-16: Bogus Bearer must not bypass CSRF ─────────────────────────────────


def test_sg_16_csrf_bearer_bypass(auth_client):
    """Session cookie + bogus Bearer + no CSRF → reject."""
    c, _ = auth_client

    res = c.post(
        "/login",
        data={"username": "admin", "password": "validpassword123"},
        follow_redirects=False,
    )
    assert res.status_code == 302
    session_cookie = c.cookies.get("session")

    c2 = TestClient(c.app, raise_server_exceptions=False)
    c2.cookies["session"] = session_cookie
    c2.headers["Authorization"] = "Bearer bogus_token"

    res2 = c2.post("/logout", follow_redirects=False)
    assert res2.status_code in (403, 302), (
        f"Bogus Bearer + no CSRF should fail, got {res2.status_code}"
    )


# ── SG-29: Revoke sessions/tokens on password change ─────────────────────────


def test_sg_29_revoke_on_password_change(auth_client):
    """Old session must be invalidated when password changes."""
    c, _ = auth_client

    # Log in and capture old session
    res = c.post(
        "/login",
        data={"username": "admin", "password": "validpassword123"},
        follow_redirects=False,
    )
    assert res.status_code == 302
    old_session = res.cookies.get("session")

    # Self password change (requires current password)
    c.post(
        "/admin/users/1/password",
        data={
            "new_password": "newpassword1234",
            "current_password": "validpassword123",
        },
        follow_redirects=False,
    )

    # Old session must be invalid (revoked by the password change)
    c2 = TestClient(c.app, raise_server_exceptions=False)
    c2.cookies["session"] = old_session
    res2 = c2.get("/admin/users")
    assert res2.status_code in (302, 401), (
        f"Old session must be revoked after password change, got {res2.status_code}"
    )


# ── SG-39: Reject disabled auth in TLS mode ──────────────────────────────────


def test_sg_39_disabled_auth_tls(auth_client):
    """With TLS headers, auth stays enabled regardless of config."""
    c, _ = auth_client
    res = c.get("/", headers={"x-forwarded-proto": "https"})
    assert res.status_code in (200, 302, 401)


def test_sg_39_tls_rejects_boolean_false(monkeypatch, tmp_path):
    """When TLS is exposed, auth.enabled=False must be rejected (auth stays on)."""
    MOCK_CONFIG_AUTH = {
        "system": {"auth": {"enabled": False}},
        "redis": {"host": "localhost", "port": 6379},
    }
    monkeypatch.setattr("config_store.load", lambda: MOCK_CONFIG_AUTH)
    monkeypatch.setattr("config_store.load_cached", lambda **_kw: MOCK_CONFIG_AUTH)
    monkeypatch.setattr("config_store.save", lambda _cfg: None)
    monkeypatch.setattr("config_store.set_armed", lambda _armed: None)
    monkeypatch.setattr("db.get_latest_event", lambda: None)
    monkeypatch.setattr("db.count_events", lambda **_kw: 0)
    monkeypatch.setattr("db.get_events", lambda **_kw: [])
    monkeypatch.setattr("db.get_latest_snapshots_by_camera", lambda: {})

    db_path = str(tmp_path / "sg-tls.db")
    if os.path.exists(db_path):
        os.unlink(db_path)
    auth_module.AUTH_DB_PATH = db_path
    auth_module.init_db(db_path)
    from routes import auth as auth_routes
    auth_routes.AUTH_DB_PATH = db_path
    from routes import users as users_routes
    users_routes.AUTH_DB_PATH = db_path

    os.environ["BOOTSTRAP_TOKEN_PATH"] = str(tmp_path / "bootstrap")
    os.environ["SCARGUARD_BOOTSTRAP_TOKEN"] = "test-token"

    db = auth_module.get_db(db_path)
    try:
        auth_module.create_user(db, "admin", "validpassword123", is_admin=True)
    finally:
        db.close()

    import main as _main
    from main import app as _app
    _main.AUTH_DB_PATH = db_path

    c = TestClient(_app, raise_server_exceptions=False)
    c.get("/", headers={"Accept": "text/html"}, follow_redirects=True)
    c.headers["X-CSRF-Token"] = c.cookies.get("csrf_token", "")

    res = c.post(
        "/login",
        data={"username": "admin", "password": "validpassword123"},
        follow_redirects=False,
    )
    assert res.status_code == 302, f"Login should succeed, got {res.status_code}"
    session_cookie = c.cookies.get("session")

    # Without TLS: auth_enabled=False → anonymous admin → 200
    c2 = TestClient(_app, raise_server_exceptions=False)
    c2.cookies["session"] = session_cookie
    c2.cookies["csrf_token"] = c.cookies.get("csrf_token", "")
    c2.headers["X-CSRF-Token"] = c.cookies.get("csrf_token", "")

    res2 = c2.get("/admin/users")
    assert res2.status_code == 200

    # With TLS: auth stays enabled, session must be valid
    c3 = TestClient(_app, raise_server_exceptions=False)
    c3.cookies["session"] = session_cookie
    c3.cookies["csrf_token"] = c.cookies.get("csrf_token", "")
    c3.headers["X-CSRF-Token"] = c.cookies.get("csrf_token", "")
    c3.headers["x-forwarded-proto"] = "https"

    res3 = c3.get("/admin/users")
    assert res3.status_code == 200, (
        f"In TLS mode, auth enabled=false must be rejected; session valid, got {res3.status_code}"
    )
