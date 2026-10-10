
import config_store
import pytest
from fastapi.testclient import TestClient
from main import app


@pytest.fixture(autouse=True)
def setup_config(tmp_path, monkeypatch):
    cfg_path = tmp_path / "scarguard.yml"
    with open(cfg_path, "w") as f:
        f.write("system:\n  revision: 1\n  armed: true\n  auth:\n    enabled: false\n")
    monkeypatch.setattr(config_store, "CONFIG_PATH", cfg_path)
    auth_db_path = str(tmp_path / "auth.db")
    monkeypatch.setattr("auth.AUTH_DB_PATH", auth_db_path)
    monkeypatch.setattr("main.AUTH_DB_PATH", auth_db_path)
    monkeypatch.setattr("routes.auth.AUTH_DB_PATH", auth_db_path)
    monkeypatch.setattr("auth.users_exist", lambda path=None: True)
    monkeypatch.setattr("main._verify_csrf_token", lambda token: True)

    # We patch it directly on the app instance if possible, or just mock route_auth and bypass the middleware entirely by not returning 401
    monkeypatch.setattr("route_auth.current_user", lambda req: getattr(req.state, "user", {"username": "admin", "role": "admin"}))
    monkeypatch.setattr("route_auth.current_role", lambda req: getattr(req.state, "user", {"role": "admin"}).get("role", "admin"))
    monkeypatch.setattr("routes.auth.BOOTSTRAP_TOKEN_PATH", str(tmp_path / "bootstrap_token"))
    monkeypatch.setattr("secret_box.DEFAULT_KEY_PATH", str(tmp_path / "secret_key"))
    monkeypatch.setattr("config_backup.BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("main.SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setattr("main.MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONFIG_PATH", str(cfg_path))

    import auth
    import main
    main.auth_module.AUTH_DB_PATH = auth_db_path
    auth.init_db(auth_db_path)

    # Also patch TestClient to trigger startup events
    with TestClient(app) as client:
        yield client, cfg_path

def test_optimistic_revision_prevents_stale_save(setup_config):
    client, cfg_path = setup_config
    # Simulate a user loading the form at revision 1
    stale_payload = {
        "system": {
            "revision": 1,
            "armed": False,
            "log_level": "info",
            "schedule": {"enabled": False},
            "auth": {"enabled": False},
            "camera_health": {},
            "backup": {},
            "summary_report": {},
            "config_api": {},
            "uploads": {"model_mb": 100, "dataset_mb": 100}
        },
        "cameras": [],
        "detection": {},
        "notifications": {"channels": []},
        "tls": {},
        "deterrent": {},
        "training": {}
    }

    # Meanwhile, another user updates the config to revision 2
    cfg = config_store.load()
    config_store.save(cfg) # bumps to 2

    # The first user tries to save their stale payload
    resp = client.post(
        "/config/structured",
        json=stale_payload,
        cookies={"session_id": "test", "csrf_token": "test-csrf"},
        headers={"x-csrf-token": "test-csrf"}
    )
    assert resp.status_code == 409
    assert "modified by another user" in resp.json()["error"]

def test_invalid_rule_missing_class_name(setup_config):
    client, cfg_path = setup_config
    # Payload with missing class_name
    payload = {
        "system": {
            "revision": 2,
            "uploads": {"model_mb": 100, "dataset_mb": 100}
        },
        "cameras": [
            {
                "name": "cam1",
                "rtsp_url": "rtsp://localhost",
                "notification_rules": [{"channels": ["email"]}] # missing class_name
            }
        ],
        "detection": {},
        "notifications": {"channels": []},
        "tls": {},
        "deterrent": {},
        "training": {}
    }
    resp = client.post(
        "/config/structured",
        json=payload,
        cookies={"session_id": "test", "csrf_token": "test-csrf"},
        headers={"x-csrf-token": "test-csrf"}
    )
    assert resp.status_code == 422

def test_rearm_deadline_persisted_in_config(setup_config, monkeypatch):
    client, cfg_path = setup_config

    # Mock current_role to return "user" so we can trigger nonadmin rearm
    import route_auth
    monkeypatch.setattr(route_auth, "current_user", lambda req: {"username": "testuser", "role": "user"})
    monkeypatch.setattr(route_auth, "current_role", lambda req: "user")

    # Disarm via dashboard
    resp = client.post(
        "/disarm",
        cookies={"session_id": "test", "csrf_token": "test-csrf"},
        headers={"x-csrf-token": "test-csrf"}
    )
    assert resp.status_code == 200

    # Verify rearm_at is persisted in the config file
    cfg = config_store.load()
    assert cfg.get("system", {}).get("armed") is False
    assert cfg.get("system", {}).get("auth", {}).get("rearm_at") is not None
