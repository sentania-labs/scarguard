"""Web route smoke tests — every page must load, core interactions must work."""

import pytest


class TestDashboard:
    def test_loads(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Dashboard" in resp.text

    def test_shows_armed_badge(self, client):
        resp = client.get("/")
        assert "ARMED" in resp.text

    def test_arm_returns_badge_fragment(self, client):
        resp = client.post("/arm")
        assert resp.status_code == 200
        assert "ARMED" in resp.text
        # Must be a partial, not a full page
        assert "<html" not in resp.text

    def test_disarm_returns_badge_fragment(self, client):
        resp = client.post("/disarm")
        assert resp.status_code == 200
        assert "DISARMED" in resp.text
        assert "<html" not in resp.text

    def test_disarm_calls_set_armed(self, client, monkeypatch):
        calls = []
        monkeypatch.setattr("config_store.set_armed", lambda v: calls.append(v))
        client.post("/disarm")
        assert calls == [False]

    def test_arm_calls_set_armed(self, client, monkeypatch):
        calls = []
        monkeypatch.setattr("config_store.set_armed", lambda v: calls.append(v))
        client.post("/arm")
        assert calls == [True]


class TestEvents:
    def test_page_loads(self, client):
        resp = client.get("/events")
        assert resp.status_code == 200
        assert "Events" in resp.text

    def test_empty_state(self, client):
        resp = client.get("/events")
        assert "No events yet" in resp.text

    def test_pagination_param_accepted(self, client):
        resp = client.get("/events?page=2")
        assert resp.status_code == 200

    def test_rows_partial(self, client):
        resp = client.get("/events/rows")
        assert resp.status_code == 200
        # Partial must not include full HTML skeleton
        assert "<!doctype" not in resp.text.lower()


class TestConfig:
    def test_page_loads(self, client):
        resp = client.get("/config")
        assert resp.status_code == 200
        assert "<textarea" in resp.text

    def test_cameras_json_is_not_html_escaped(self, client):
        resp = client.get("/config")
        assert resp.status_code == 200
        assert '"rtsp_url": "rtsp://localhost/test"' in resp.text
        assert "&#34;rtsp_url&#34;" not in resp.text

    def test_post_valid_yaml(self, client, monkeypatch):
        saved = []
        monkeypatch.setattr("config_store.save", lambda cfg: saved.append(cfg))
        resp = client.post("/config", data={"raw_yaml": "system:\n  armed: false\n"})
        assert resp.status_code == 200
        assert "saved" in resp.text.lower() or len(saved) == 1

    def test_post_invalid_yaml_shows_error(self, client):
        resp = client.post("/config", data={"raw_yaml": ": : invalid: yaml: ["})
        assert resp.status_code == 200
        assert "Error" in resp.text or "error" in resp.text

    def test_post_non_mapping_yaml_shows_error(self, client):
        resp = client.post("/config", data={"raw_yaml": "- just\n- a\n- list\n"})
        assert resp.status_code == 200
        assert "Error" in resp.text or "error" in resp.text.lower()

    def test_page_populates_form_with_config_values(self, client, monkeypatch):
        """Form fields must reflect values from the config file, not defaults."""
        cfg = {
            "system": {"armed": False, "log_level": "debug"},
            "cameras": [
                {"name": "pond-north", "rtsp_url": "rtsp://localhost/test", "enabled": True, "resolution": 720}
            ],
            "detection": {
                "model_path": "/models/custom.pt",
                "confidence_threshold": 0.42,
                "target_classes": ["great_blue_heron"],
                "cooldown_seconds": 30,
                "frame_skip": 2,
            },
            "notifications": {
                "discord": {"enabled": True, "webhook_url": "https://discord.com/api/webhooks/test", "mention_role": "", "include_snapshot": True},
                "email": {"enabled": False, "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_pass": "", "to_addresses": [], "include_snapshot": True},
            },
        }
        monkeypatch.setattr("config_store.load", lambda: cfg)
        resp = client.get("/config")
        assert resp.status_code == 200
        # System: armed=False → checkbox must NOT have checked attribute
        assert 'id="sys-armed"' in resp.text
        assert 'id="sys-armed" checked' not in resp.text
        # Detection: custom model path and confidence
        assert "/models/custom.pt" in resp.text
        assert "0.42" in resp.text
        # Discord webhook URL populated
        assert "https://discord.com/api/webhooks/test" in resp.text

    def test_page_partial_fallback_on_invalid_camera(self, client, monkeypatch):
        """A camera with an invalid name should be skipped, but other sections still populate."""
        cfg = {
            "system": {"armed": True, "log_level": "warning"},
            "cameras": [
                {"name": "", "rtsp_url": "rtsp://localhost/bad", "enabled": True, "resolution": 720},
            ],
            "detection": {
                "model_path": "/models/best.pt",
                "confidence_threshold": 0.25,
                "target_classes": [],
                "cooldown_seconds": 30,
                "frame_skip": 2,
            },
            "notifications": {},
        }
        monkeypatch.setattr("config_store.load", lambda: cfg)
        resp = client.get("/config")
        assert resp.status_code == 200
        # Page must still render with the system log level from config (not default "info")
        assert "warning" in resp.text
        # The bad camera should be absent from cameras_json (empty array)
        assert '"cameras-data"' in resp.text or "cameras-data" in resp.text
        # cameras_json should be empty because the only camera had an invalid name
        assert "[]" in resp.text

    @pytest.mark.parametrize("bad_cameras_value", [None, "rtsp://localhost/one", 123])
    def test_page_handles_non_list_cameras_section(self, client, monkeypatch, bad_cameras_value):
        """Malformed cameras sections should not crash /config and should fall back gracefully."""
        cfg = {
            "system": {"armed": True, "log_level": "warning"},
            "cameras": bad_cameras_value,
            "detection": {"model_path": "/models/best.pt"},
            "notifications": {},
        }
        monkeypatch.setattr("config_store.load", lambda: cfg)
        resp = client.get("/config")
        assert resp.status_code == 200
        # Non-camera sections should still render from config.
        assert "warning" in resp.text
        # cameras_json should fall back to an empty list.
        assert "[]" in resp.text

    def test_structured_save_preserves_exclusion_zones(self, client, monkeypatch):
        """Saving via the structured form must not drop exclusion_zones from cameras."""
        existing = {
            "system": {"armed": True, "log_level": "info"},
            "cameras": [
                {
                    "name": "pond-north",
                    "rtsp_url": "rtsp://localhost/test",
                    "enabled": True,
                    "resolution": 720,
                    "exclusion_zones": [{"x": 10, "y": 20, "w": 50, "h": 60, "label": "decoy"}],
                }
            ],
            "detection": {
                "model_path": "/models/best.pt",
                "confidence_threshold": 0.25,
                "target_classes": [],
                "cooldown_seconds": 30,
                "frame_skip": 2,
            },
            "notifications": {},
            "redis": {"host": "redis", "port": 6379},
        }
        saved_cfgs = []
        monkeypatch.setattr("config_store.load", lambda: existing)
        monkeypatch.setattr("config_store.save", lambda cfg: saved_cfgs.append(cfg))

        payload = {
            "system": {"armed": True, "log_level": "info"},
            "cameras": [
                {"name": "pond-north", "rtsp_url": "rtsp://localhost/test", "enabled": True, "resolution": 720}
            ],
            "detection": {
                "model_path": "/models/best.pt",
                "confidence_threshold": 0.25,
                "target_classes": [],
                "cooldown_seconds": 30,
                "frame_skip": 2,
            },
            "notifications": {
                "discord": {"enabled": False, "webhook_url": "", "mention_role": "", "include_snapshot": True},
                "email": {"enabled": False, "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_pass": "", "to_addresses": [], "include_snapshot": True},
            },
        }
        resp = client.post("/config/structured", json=payload)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert len(saved_cfgs) == 1
        saved_cam = saved_cfgs[0]["cameras"][0]
        assert "exclusion_zones" in saved_cam, "exclusion_zones were dropped on save"
        assert saved_cam["exclusion_zones"] == [{"x": 10, "y": 20, "w": 50, "h": 60, "label": "decoy"}]

    def test_structured_save_preserves_redis_and_unknown_keys(self, client, monkeypatch):
        """Keys the form doesn't touch (redis, webhooks) must survive a structured save."""
        existing = {
            "system": {"armed": True, "log_level": "info"},
            "cameras": [],
            "detection": {
                "model_path": "/models/best.pt",
                "confidence_threshold": 0.25,
                "target_classes": [],
                "cooldown_seconds": 30,
                "frame_skip": 2,
            },
            "notifications": {},
            "redis": {"host": "redis", "port": 6379},
            "webhooks": [{"url": "https://example.test/hook", "method": "POST"}],
        }
        saved_cfgs = []
        monkeypatch.setattr("config_store.load", lambda: existing)
        monkeypatch.setattr("config_store.save", lambda cfg: saved_cfgs.append(cfg))

        payload = {
            "system": {"armed": False, "log_level": "debug"},
            "cameras": [],
            "detection": {
                "model_path": "/models/best.pt",
                "confidence_threshold": 0.25,
                "target_classes": [],
                "cooldown_seconds": 30,
                "frame_skip": 2,
            },
            "notifications": {
                "discord": {"enabled": False, "webhook_url": "", "mention_role": "", "include_snapshot": True},
                "email": {"enabled": False, "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_pass": "", "to_addresses": [], "include_snapshot": True},
            },
        }
        resp = client.post("/config/structured", json=payload)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        saved = saved_cfgs[0]
        assert saved["redis"] == {"host": "redis", "port": 6379}
        assert "webhooks" in saved

    def test_structured_save_preserves_existing_timezone_when_omitted(self, client, monkeypatch):
        """Structured saves that omit system.timezone must not reset it to UTC."""
        existing = {
            "system": {"armed": True, "log_level": "info", "timezone": "America/New_York"},
            "cameras": [],
            "detection": {
                "model_path": "/models/best.pt",
                "confidence_threshold": 0.25,
                "target_classes": [],
                "cooldown_seconds": 30,
                "frame_skip": 2,
            },
            "notifications": {},
        }
        saved_cfgs = []
        monkeypatch.setattr("config_store.load", lambda: existing)
        monkeypatch.setattr("config_store.save", lambda cfg: saved_cfgs.append(cfg))

        payload = {
            "system": {"armed": False, "log_level": "debug"},
            "cameras": [],
            "detection": {
                "model_path": "/models/best.pt",
                "confidence_threshold": 0.25,
                "target_classes": [],
                "cooldown_seconds": 30,
                "frame_skip": 2,
            },
            "notifications": {
                "discord": {"enabled": False, "webhook_url": "", "mention_role": "", "include_snapshot": True},
                "email": {"enabled": False, "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_pass": "", "to_addresses": [], "include_snapshot": True},
            },
        }
        resp = client.post("/config/structured", json=payload)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        saved = saved_cfgs[0]
        assert saved["system"]["armed"] is False
        assert saved["system"]["log_level"] == "debug"
        assert saved["system"]["timezone"] == "America/New_York"


class TestConfigErrorScrubbing:
    """Issue #95 — exception messages must not leak through the JSON body."""

    def test_invalid_payload_returns_generic_message_and_request_id(self, client, monkeypatch):
        # Force pydantic to fail by sending nonsense for a typed field.
        bad_payload = {
            "system": {"armed": True, "log_level": "info"},
            "cameras": [],
            "detection": {
                "model_path": "/models/best.pt",
                "confidence_threshold": "not-a-float",  # type error
                "target_classes": [],
                "cooldown_seconds": 30,
                "frame_skip": 2,
            },
            "notifications": {
                "discord": {"enabled": False, "webhook_url": "", "mention_role": "", "include_snapshot": True},
                "email": {"enabled": False, "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_pass": "", "to_addresses": [], "include_snapshot": True},
            },
        }
        resp = client.post("/config/structured", json=bad_payload)
        assert resp.status_code == 422
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "Invalid config payload"
        assert "request_id" in body and len(body["request_id"]) == 8
        # Ensure no pydantic internals leaked through.
        assert "ValidationError" not in resp.text
        assert "confidence_threshold" not in resp.text

    def test_invalid_json_body_returns_generic_message(self, client):
        # Hits the generic Exception branch (json() raises before pydantic
        # ever runs) — must also be scrubbed.
        resp = client.post(
            "/config/structured",
            content=b"this is not json {{{",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "Unable to save config"
        assert "request_id" in body and len(body["request_id"]) == 8


class TestTLSCertContainment:
    """Issue #95 — defense-in-depth: writes to _CERTS_DIR must stay inside it."""

    def test_upload_writes_into_certs_dir(self, client, monkeypatch, tmp_path):
        from routes import config as config_route

        certs_dir = tmp_path / "certs"
        monkeypatch.setattr(config_route, "_CERTS_DIR", certs_dir)
        pem = b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n"
        resp = client.post(
            "/config/tls/upload-cert",
            files={"cert_file": ("ignored-by-server.pem", pem, "application/x-pem-file")},
        )
        assert resp.status_code == 200, resp.text
        assert (certs_dir / "cert.pem").exists()
        # Confirm nothing escaped the directory.
        for p in certs_dir.iterdir():
            assert p.parent.resolve() == certs_dir.resolve()


class TestModels:
    def test_page_loads(self, client):
        resp = client.get("/models")
        assert resp.status_code == 200
        assert "Upload" in resp.text

    def test_upload_rejects_bad_extension(self, client):
        resp = client.post(
            "/models",
            files={"file": ("malware.exe", b"bad", "application/octet-stream")},
        )
        assert resp.status_code == 200
        assert "Unsupported" in resp.text

    def test_upload_pt_file(self, client, tmp_path):
        content = b"fake model weights"
        resp = client.post(
            "/models",
            files={"file": ("test.pt", content, "application/octet-stream")},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "test.pt" in resp.text
