import pytest
from fastapi.testclient import TestClient
import auth as auth_module
import os

@pytest.fixture
def auth_client(monkeypatch, tmp_path):
    # Enable auth for this test
    MOCK_CONFIG_AUTH = {
        "system": {"auth": {"enabled": True}},
        "redis": {"host": "localhost", "port": 6379},
    }
    monkeypatch.setattr("config_store.load", lambda: MOCK_CONFIG_AUTH)
    monkeypatch.setattr("config_store.load_cached", lambda **_kw: MOCK_CONFIG_AUTH)
    monkeypatch.setattr("config_store.save", lambda _cfg: None)
    
    db_path = "/tmp/sg-test-auth.db"
    import os
    if os.path.exists(db_path): os.unlink(db_path)
    auth_module.init_db(db_path)
    
    # We also need bootstrap token for setup if users don't exist
    token_path = str(tmp_path / "bootstrap")
    os.environ["BOOTSTRAP_TOKEN_PATH"] = token_path
    os.environ["SCARGUARD_BOOTSTRAP_TOKEN"] = "test-token"
    
    # Initialize a user
    db = auth_module.get_db(db_path)
    uid = auth_module.create_user(db, "admin", "validpassword123", is_admin=True)
    db.close()
    
    from fastapi.testclient import TestClient
    from main import app
    c = TestClient(app)
    c.get("/")
    c.headers["X-CSRF-Token"] = c.cookies.get("csrf_token", "")
    
    return c, db_path

def test_sg_13_bound_username_password_size(auth_client):
    c, _ = auth_client
    long_str = "a" * 1000
    res = c.post("/login", data={"username": long_str, "password": "abc"})
    assert res.status_code == 400
    
def test_sg_15_origin_user_lockout(auth_client):
    c, db_path = auth_client
    # Simulate attacker failing from IP1
    c1 = TestClient(c.app, client=("10.0.0.1", 12345))
    c1.headers["X-CSRF-Token"] = c.headers["X-CSRF-Token"]
    c1.cookies = c.cookies
    for _ in range(5):
        c1.post("/login", data={"username": "admin", "password": "wrong"})
    
    c2 = TestClient(c.app, client=("10.0.0.2", 12345))
    c2.headers["X-CSRF-Token"] = c.headers["X-CSRF-Token"]
    c2.cookies = c.cookies
    res = c2.post("/login", data={"username": "admin", "password": "validpassword123"}, follow_redirects=False)
    assert res.status_code == 302, "Legitimate user was locked out by attacker on different IP"

def test_sg_16_csrf_bearer_bypass(auth_client):
    c, _ = auth_client
    # Log in
    res = c.post("/login", data={"username": "admin", "password": "validpassword123"}, follow_redirects=False)
    assert res.status_code == 302
    session_cookie = c.cookies.get("session")
    
    # Attempt CSRF by passing a bogus Bearer token
    c2 = TestClient(c.app)
    c2.cookies["session"] = session_cookie
    c2.headers["Authorization"] = "Bearer bogus_token"
    
    # Post to a mutating endpoint, e.g. /logout without CSRF token
    res2 = c2.post("/logout", follow_redirects=False)
    # Should not succeed (should be 403 or redirect to login)
    assert res2.status_code in (403, 302)

def test_sg_29_revoke_on_password_change(auth_client):
    c, _ = auth_client
    res = c.post("/login", data={"username": "admin", "password": "validpassword123"}, follow_redirects=False)
    session_cookie = res.cookies.get("session")
    
    c.post("/admin/users/1/password", data={"new_password": "newpassword123", "current_password": "validpassword123"}, follow_redirects=False)
    
    c2 = TestClient(c.app)
    c2.cookies["session"] = session_cookie
    res2 = c2.get("/admin/users")
    assert res2.status_code in (302, 401) # Old session should be invalid and redirect to login

def test_sg_39_disabled_auth_tls(auth_client):
    c, _ = auth_client
    # This might require MOCK_CONFIG with enabled: "false" instead of boolean
    pass

