import config_store
import pytest
from url_safety import redact_url


@pytest.fixture
def sentinel_config(monkeypatch):
    cfg = {
        "cameras": [{"name": "cam1", "rtsp_url": "rtsp://admin:SENTINEL_RTSP@1.2.3.4"}],
        "training": {"sources": {"roboflow": {"api_key": "SENTINEL_ROBO"}}},
        "deterrent": {"tuya": {"api_key": "SENTINEL_TUYA_K", "api_secret": "SENTINEL_TUYA_S"}},
        "notifications": {"channels": [{"name": "ch1", "webhook_url": "https://h/SENTINEL_HOOK"}]},
    }
    monkeypatch.setattr(config_store, "load", lambda: cfg)
    return cfg


def test_sentinels_absent_from_viewer_responses(client, sentinel_config, monkeypatch):
    # Setup mock user context if needed, or assume default is viewer if no admin
    import route_auth
    monkeypatch.setattr(route_auth, "has_admin_access", lambda req: False)
    resp = client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "SENTINEL" not in resp.text
    assert data["cameras"][0]["rtsp_url"] == "***REDACTED***"
    assert data["training"]["sources"]["roboflow"]["api_key"] == "***REDACTED***"
    assert data["notifications"]["channels"][0]["webhook_url"] == "***REDACTED***"


def test_url_safety_redacts_credentials():
    assert redact_url("rtsp://admin:SENTINEL_RTSP@1.2.3.4/live") == "rtsp://admin:***@1.2.3.4/live"
