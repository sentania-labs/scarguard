"""Tests for services/web/src/config_redact.py."""
from __future__ import annotations

import pytest
import yaml

from config_redact import REDACTED_PLACEHOLDER, redact_config, redact_yaml


@pytest.fixture
def full_config() -> dict:
    """A realistic scarguard.yml with every sensitive-field type populated."""
    return {
        "system": {"log_level": "info", "retention_days": 90},
        "cameras": [
            {
                "name": "pond-south",
                "rtsp_url": "rtsps://172.16.0.1:7441/SECRET_A?enableSrtp",
                "enabled": True,
            },
            {
                "name": "pond-north",
                "rtsp_url": "rtsps://172.16.0.1:7441/SECRET_B?enableSrtp",
                "enabled": True,
            },
            {"name": "unused", "rtsp_url": "", "enabled": False},
        ],
        "notifications": {
            "discord": {
                "enabled": True,
                "webhook_url": "https://discord.com/api/webhooks/111/TOP_SECRET",
            },
            "email": {
                "enabled": True,
                "smtp_host": "smtp.example.com",
                "smtp_user": "ops@example.com",
                "smtp_pass": "hunter2",
            },
            "channels": [
                {
                    "name": "bird-discord",
                    "type": "discord",
                    "webhook_url": "https://discord.com/api/webhooks/222/CHANNEL_SECRET",
                },
                {
                    "name": "bird-email",
                    "type": "email",
                    "smtp_host": "smtp.example.com",
                    "smtp_pass": "channel-pw",
                },
                {
                    "name": "home-assistant",
                    "type": "webhook",
                    "url": "https://ha.local/webhook",
                    "auth_token": "bearer-abc",
                    "headers": {"X-API-Key": "also-secret"},
                },
                {
                    "name": "push",
                    "type": "ntfy",
                    "server": "https://ntfy.sh",
                    "topic": "my-alerts",
                    "token": "tk_xyz",
                    "password": "basic-pw",
                },
            ],
        },
        "tls": {"mode": "auto", "domain": "scarguard.example.net"},
    }


class TestRedactConfig:
    def test_masks_camera_rtsp_urls(self, full_config):
        red = redact_config(full_config)
        assert red["cameras"][0]["rtsp_url"] == REDACTED_PLACEHOLDER
        assert red["cameras"][1]["rtsp_url"] == REDACTED_PLACEHOLDER
        # Empty rtsp_url stays empty (not masked — nothing to hide)
        assert red["cameras"][2]["rtsp_url"] == ""

    def test_masks_discord_webhook(self, full_config):
        red = redact_config(full_config)
        assert red["notifications"]["discord"]["webhook_url"] == REDACTED_PLACEHOLDER

    def test_masks_email_password(self, full_config):
        red = redact_config(full_config)
        assert red["notifications"]["email"]["smtp_pass"] == REDACTED_PLACEHOLDER
        # Non-secret fields stay visible
        assert red["notifications"]["email"]["smtp_host"] == "smtp.example.com"
        assert red["notifications"]["email"]["smtp_user"] == "ops@example.com"

    def test_masks_named_channels(self, full_config):
        red = redact_config(full_config)
        channels = {c["name"]: c for c in red["notifications"]["channels"]}
        assert channels["bird-discord"]["webhook_url"] == REDACTED_PLACEHOLDER
        assert channels["bird-email"]["smtp_pass"] == REDACTED_PLACEHOLDER
        assert channels["home-assistant"]["auth_token"] == REDACTED_PLACEHOLDER
        # headers stays a dict; values matching the sensitive-header
        # heuristic (auth/token/key/secret/cookie/password) are masked,
        # other values stay visible.
        assert isinstance(channels["home-assistant"]["headers"], dict)
        assert channels["home-assistant"]["headers"]["X-API-Key"] == REDACTED_PLACEHOLDER
        assert channels["push"]["token"] == REDACTED_PLACEHOLDER
        assert channels["push"]["password"] == REDACTED_PLACEHOLDER
        # Non-secret fields stay visible
        assert channels["home-assistant"]["url"] == "https://ha.local/webhook"
        assert channels["push"]["topic"] == "my-alerts"
        assert channels["push"]["server"] == "https://ntfy.sh"

    def test_masks_header_values_not_structure(self):
        """Custom header dict keeps its shape; only credential values masked."""
        cfg = {
            "notifications": {
                "channels": [{
                    "name": "hook",
                    "type": "webhook",
                    "url": "https://example.com",
                    "headers": {
                        "Content-Type": "application/json",
                        "X-Source": "scarguard",
                        "Authorization": "Bearer secret123",
                        "X-API-Key": "key456",
                        "Cookie": "session=abc",
                    },
                }]
            }
        }
        red = redact_config(cfg)
        hdrs = red["notifications"]["channels"][0]["headers"]
        assert isinstance(hdrs, dict)
        # Non-sensitive routing headers stay visible
        assert hdrs["Content-Type"] == "application/json"
        assert hdrs["X-Source"] == "scarguard"
        # Credential headers masked
        assert hdrs["Authorization"] == REDACTED_PLACEHOLDER
        assert hdrs["X-API-Key"] == REDACTED_PLACEHOLDER
        assert hdrs["Cookie"] == REDACTED_PLACEHOLDER

    def test_does_not_mutate_input(self, full_config):
        """redact_config must deep-copy, not mutate its input."""
        before = dict(full_config)
        red = redact_config(full_config)
        # Input still has the secrets
        assert full_config["cameras"][0]["rtsp_url"].startswith("rtsps://")
        assert full_config["notifications"]["email"]["smtp_pass"] == "hunter2"
        assert full_config is not red
        assert id(full_config["cameras"]) != id(red["cameras"])
        # Just a sanity check that the top-level keys are unchanged
        assert set(full_config.keys()) == set(before.keys())

    def test_leaves_non_sensitive_untouched(self, full_config):
        red = redact_config(full_config)
        assert red["system"] == full_config["system"]
        assert red["tls"] == full_config["tls"]

    def test_idempotent(self, full_config):
        red1 = redact_config(full_config)
        red2 = redact_config(red1)
        assert red1 == red2

    def test_handles_missing_sections(self):
        minimal = {"system": {"log_level": "info"}}
        red = redact_config(minimal)
        assert red == minimal
        # Doesn't raise if cameras is missing either
        assert redact_config({}) == {}

    def test_handles_non_dict_input(self):
        assert redact_config([]) == []  # type: ignore[arg-type]
        assert redact_config("a string") == "a string"  # type: ignore[arg-type]
        assert redact_config(None) is None  # type: ignore[arg-type]

    def test_handles_empty_cameras_list(self):
        cfg = {"cameras": []}
        red = redact_config(cfg)
        assert red == cfg

    def test_handles_empty_channels_list(self):
        cfg = {"notifications": {"channels": []}}
        red = redact_config(cfg)
        assert red == cfg

    def test_handles_malformed_channel_entries(self):
        """A channel entry that isn't a dict should be silently skipped."""
        cfg = {
            "notifications": {
                "channels": [
                    "not-a-dict",  # should be ignored
                    {"name": "real", "webhook_url": "https://example.com/secret"},
                ]
            }
        }
        red = redact_config(cfg)
        assert red["notifications"]["channels"][0] == "not-a-dict"
        assert red["notifications"]["channels"][1]["webhook_url"] == REDACTED_PLACEHOLDER

    def test_custom_placeholder(self, full_config):
        red = redact_config(full_config, placeholder="<hidden>")
        assert red["cameras"][0]["rtsp_url"] == "<hidden>"


class TestRedactYaml:
    def test_round_trips_and_masks(self, full_config):
        yaml_text = yaml.safe_dump(full_config)
        redacted = redact_yaml(yaml_text)
        # None of the real secrets should appear in the redacted YAML
        assert "SECRET_A" not in redacted
        assert "SECRET_B" not in redacted
        assert "TOP_SECRET" not in redacted
        assert "CHANNEL_SECRET" not in redacted
        assert "hunter2" not in redacted
        assert "bearer-abc" not in redacted
        assert "also-secret" not in redacted
        assert "tk_xyz" not in redacted
        assert "basic-pw" not in redacted
        assert "channel-pw" not in redacted
        # The placeholder does appear
        assert REDACTED_PLACEHOLDER in redacted
        # And the result is valid YAML
        reparsed = yaml.safe_load(redacted)
        assert reparsed["cameras"][0]["rtsp_url"] == REDACTED_PLACEHOLDER

    def test_leaves_invalid_yaml_alone(self):
        # Malformed YAML should pass through unchanged rather than crash.
        broken = "this is: [ not valid yaml"
        assert redact_yaml(broken) == broken

    def test_non_dict_yaml_returned_unchanged(self):
        # YAML that parses to a list (not a dict) shouldn't blow up.
        listy = "- one\n- two\n"
        assert redact_yaml(listy) == listy
