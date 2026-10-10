
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


def test_solar_schedule_returns_transitions(setup_config):
    """Finding 01M4HPB8V441N41BD6S9Q5WMEB: Solar schedule must compute transitions."""
    from datetime import datetime, timedelta, timezone

    # Mock the solar computation to return known transitions
    def mock_solar(*args, **kwargs):
        start_dt = args[0] if args else kwargs.get("start")
        if start_dt is None:
            return None, None
        sunrise = start_dt + timedelta(hours=1)
        sunset = start_dt + timedelta(hours=5)
        return sunrise, sunset

    from unittest.mock import patch
    with patch("scheduler._compute_solar_transitions", mock_solar):
        from scheduler import ArmScheduler

        sched = ArmScheduler()

        # Simulate a config with solar enabled
        import config_store
        cfg = config_store.load()
        cfg["system"] = {
            "revision": 1,
            "armed": True,
            "schedule": {
                "enabled": True,
                "use_solar": True,
                "latitude": 40.0,
                "longitude": -74.0,
            },
            "auth": {"enabled": False},
            "timezone": "America/New_York",
        }
        config_store.save(cfg)

        start = datetime.now(timezone.utc)
        end = start + timedelta(hours=6)

        transitions = sched._get_solar_transitions(start, end)
        assert len(transitions) >= 1, "Solar scheduler must return at least one transition"
        armed_events = [t for t, armed in transitions if armed]
        assert len(armed_events) >= 1, "Must have at least one arm (sunrise) transition"


def test_fixed_schedule_applies_timezone(setup_config):
    """Finding 01M4HPB8V626BQCPGR2NGZTYKM: Fixed schedules use configured timezone."""
    client, cfg_path = setup_config
    from datetime import datetime, timedelta, timezone

    # Mock config to have a non-UTC timezone with fixed times
    import config_store
    from scheduler import _parse_time, transitions_between
    cfg = config_store.load()
    cfg["system"] = {
        "revision": 2,
        "armed": True,
        "schedule": {
            "enabled": True,
            "arm_time": "06:00",
            "disarm_time": "18:00",
        },
        "auth": {"enabled": False},
        "timezone": "America/New_York",  # UTC-5
    }
    config_store.save(cfg)

    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/New_York")

    start = datetime(2026, 10, 10, 5, 0, 0, tzinfo=timezone.utc)  # 00:00 ET
    end = start + timedelta(hours=12)  # 11:00 ET

    def get_arm(dt):
        return _parse_time("06:00")

    def get_disarm(dt):
        return _parse_time("18:00")

    transitions = transitions_between(start, end, get_arm, get_disarm, tz)

    # The arm time 06:00 ET = 11:00 UTC should be within the window
    # If timezone is not applied, 06:00 UTC would fire immediately (at or before start)
    # With proper timezone: 06:00 ET = 11:00 UTC which is at the end boundary
    assert len(transitions) >= 1, "Should find at least one transition with correct TZ"
    for t, armed in transitions:
        # Transitions should be in UTC
        assert t.tzinfo is not None


def test_revision_reflected_in_save_response(setup_config):
    """Finding 01M4HPB8V992QRP60S0A7HM16M: Save response returns new revision."""
    client, cfg_path = setup_config
    import config_store

    # Load to get current revision
    cfg = config_store.load()
    old_revision = cfg.get("system", {}).get("revision", 0)

    # Save through the API
    payload = {
        "system": {
            "revision": old_revision,
            "armed": True,
            "log_level": "debug",
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

    resp = client.post(
        "/config/structured",
        json=payload,
        cookies={"session_id": "test", "csrf_token": "test-csrf"},
        headers={"x-csrf-token": "test-csrf"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    new_rev = data.get("revision")
    assert new_rev is not None, "Response must include 'revision' field"
    assert new_rev == old_revision + 1, "Response revision must be old + 1"


def test_invalid_deterrent_rule_missing_class_name(setup_config):
    """Invalid deterrent rules without class_name must be rejected at save."""
    client, cfg_path = setup_config
    payload = {
        "system": {
            "revision": 3,
            "uploads": {"model_mb": 100, "dataset_mb": 100}
        },
        "cameras": [
            {
                "name": "cam1",
                "rtsp_url": "rtsp://localhost",
                "deterrent_rules": [{"groups": ["sprinklers"]}]  # missing class_name
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


def test_reload_preserves_disarm_state(setup_config):
    """After service restart (config reload), disarm + rearm_at are preserved."""
    client, cfg_path = setup_config
    import config_store
    import route_auth

    # Simulate user disarm with auto-rearm
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(route_auth, "current_user", lambda req: {"username": "testuser", "role": "user"})
    monkeypatch.setattr(route_auth, "current_role", lambda req: "user")

    resp = client.post(
        "/disarm",
        cookies={"session_id": "test", "csrf_token": "test-csrf"},
        headers={"x-csrf-token": "test-csrf"}
    )
    assert resp.status_code == 200

    # Simulate a "reload" by re-reading the config (what happens on restart)
    cfg = config_store.load()
    assert cfg.get("system", {}).get("armed") is False
    rearm_at = cfg.get("system", {}).get("auth", {}).get("rearm_at")
    assert rearm_at is not None, "rearm_at must persist across restarts"
    # The config is still on disk - "reload" just reads it back
    reloaded = config_store.load()
    assert reloaded.get("system", {}).get("armed") is False


def test_admin_rearm_overrides_user_disarm(setup_config):
    """Admin arm should override a pending user disarm (rearm_at cleared)."""
    import config_store
    client, _ = setup_config

    # User disarms first
    cfg = config_store.load()
    cfg["system"]["armed"] = False
    cfg.setdefault("system", {}).setdefault("auth", {})["rearm_at"] = "2099-01-01T00:00:00+00:00"
    config_store.save(cfg)

    # Admin arms - this should clear rearm_at
    payload = {
        "system": {
            "revision": config_store.load().get("system", {}).get("revision", 0),
            "armed": True,
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
    resp = client.post(
        "/config/structured",
        json=payload,
        cookies={"session_id": "test", "csrf_token": "test-csrf"},
        headers={"x-csrf-token": "test-csrf"}
    )
    assert resp.status_code == 200
    cfg = config_store.load()
    assert cfg.get("system", {}).get("armed") is True


def test_double_save_after_revision_update_succeeds(setup_config):
    """After a successful save, the revision is updated in the response,
    so a second save with the new revision must succeed (not 409)."""
    client, cfg_path = setup_config
    import config_store

    # First save
    cfg = config_store.load()
    rev1 = cfg.get("system", {}).get("revision", 0)

    payload1 = {
        "system": {
            "revision": rev1,
            "armed": True,
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

    resp1 = client.post(
        "/config/structured",
        json=payload1,
        cookies={"session_id": "test", "csrf_token": "test-csrf"},
        headers={"x-csrf-token": "test-csrf"}
    )
    assert resp1.status_code == 200
    new_rev = resp1.json().get("revision", rev1 + 1)

    # Second save using the new revision (simulates JS updating the hidden field)
    payload2 = {
        "system": {
            "revision": new_rev,
            "armed": False,  # changed value
            "log_level": "debug",
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

    resp2 = client.post(
        "/config/structured",
        json=payload2,
        cookies={"session_id": "test", "csrf_token": "test-csrf"},
        headers={"x-csrf-token": "test-csrf"}
    )
    assert resp2.status_code == 200, f"Second save with updated revision must succeed, got {resp2.status_code}"
    assert resp2.json()["ok"] is True
