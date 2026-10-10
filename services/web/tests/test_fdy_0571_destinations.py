"""FDY-0571: notification destinations are validated when config is saved,
and the per-channel LAN / plaintext opt-ins are visible in the config UI.

Exercises the real routes (structured form save, raw-YAML save, config page)
and ``validate_full_config``; only config_store I/O is stubbed.
"""

from __future__ import annotations

import copy

import pytest
import yaml
from config_model import validate_full_config

BASE = {
    "system": {"armed": True, "log_level": "info", "auth": {"enabled": False}},
    "cameras": [],
    "detection": {
        "model_path": "/models/best.pt",
        "confidence_threshold": 0.25,
        "target_classes": [],
        "cooldown_seconds": 30,
        "frame_skip": 2,
    },
}


def _payload(channels: list[dict]) -> dict:
    return {**copy.deepcopy(BASE), "notifications": {"channels": channels}}


@pytest.fixture()
def saved(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    # The save handlers merge into the loaded dict in place; give them a
    # private copy so the shared conftest MOCK_CONFIG is never mutated.
    _stored(monkeypatch, [])
    calls: list[dict] = []
    monkeypatch.setattr("config_store.save", lambda cfg: calls.append(copy.deepcopy(cfg)))
    return calls


def _error_alert(html: str) -> str:
    return html.split('class="alert alert-err"')[1].split("</div>")[0]


def _stored(monkeypatch: pytest.MonkeyPatch, channels: list[dict]) -> None:
    cfg = _payload(channels)
    monkeypatch.setattr("config_store.load", lambda: copy.deepcopy(cfg))
    monkeypatch.setattr("config_store.load_cached", lambda **_kw: copy.deepcopy(cfg))


# ── Structured form save ────────────────────────────────────────────────────


@pytest.mark.parametrize("channel", [
    {"name": "h", "type": "webhook", "url": "http://169.254.169.254/latest/meta-data/",
     "allow_internal": True},
    {"name": "h", "type": "webhook", "url": "http://127.0.0.1:6379/", "allow_internal": True},
    {"name": "h", "type": "webhook", "url": "http://redis:6379/", "allow_internal": True},
    {"name": "h", "type": "webhook", "url": "http://172.17.0.1/", "allow_internal": True},
    {"name": "n", "type": "ntfy", "topic": "t", "server": "http://172.24.0.4",
     "allow_internal": True},
    {"name": "n", "type": "ntfy", "topic": "t", "server": "file:///etc/passwd"},
    {"name": "m", "type": "email", "smtp_host": "127.0.0.1", "smtp_port": 25,
     "to_addresses": ["a@example.com"], "allow_internal": True},
    {"name": "m", "type": "email", "smtp_host": "smtp.example.com", "smtp_port": 70000,
     "to_addresses": ["a@example.com"]},
    {"name": "d", "type": "discord", "webhook_url": "http://10.0.0.5/api/webhooks/1/x"},
])
def test_structured_save_refuses_unsafe_destination(client, saved, channel):
    resp = client.post("/config/structured", json=_payload([{**channel, "enabled": True}]))
    assert resp.status_code == 422
    assert resp.json()["ok"] is False
    assert saved == []


def test_structured_save_accepts_public_destinations(client, saved):
    channels = [
        {"name": "d", "type": "discord", "enabled": True,
         "webhook_url": "https://discord.com/api/webhooks/1/tok"},
        {"name": "h", "type": "webhook", "enabled": True, "url": "https://hooks.example.com/x"},
        {"name": "n", "type": "ntfy", "enabled": True, "topic": "t", "server": "https://ntfy.sh"},
        {"name": "m", "type": "email", "enabled": True, "smtp_host": "smtp.gmail.com",
         "smtp_port": 587, "to_addresses": ["a@example.com"]},
    ]
    resp = client.post("/config/structured", json=_payload(channels))
    assert resp.status_code == 200, resp.text
    assert len(saved) == 1


def test_structured_save_skips_redacted_discord_secret(client, saved):
    resp = client.post("/config/structured", json=_payload([
        {"name": "d", "type": "discord", "enabled": True, "webhook_url": "***REDACTED***"},
    ]))
    assert resp.status_code == 200


def test_structured_save_persists_lan_opt_ins_sent_by_the_form(client, saved, monkeypatch):
    """The channel editor sends allow_internal, smtp_ca_file and
    smtp_insecure_plaintext; what it sends is what is stored."""
    form_channels = [
        {"name": "ha", "type": "webhook", "enabled": True,
         "url": "http://192.168.1.50:8123/api/webhook/pond", "method": "POST",
         "allow_internal": True},
        {"name": "lan-ntfy", "type": "ntfy", "enabled": True, "topic": "pond",
         "server": "http://192.168.1.60", "allow_internal": True},
        {"name": "relay", "type": "email", "enabled": True, "smtp_host": "192.168.1.25",
         "smtp_port": 25, "to_addresses": ["a@example.com"], "allow_internal": True,
         "smtp_insecure_plaintext": True, "smtp_ca_file": ""},
        {"name": "private-ca", "type": "email", "enabled": True, "smtp_host": "mail.example.com",
         "smtp_port": 587, "to_addresses": ["a@example.com"], "allow_internal": False,
         "smtp_insecure_plaintext": False, "smtp_ca_file": "/config/certs/smtp-ca.pem"},
    ]
    resp = client.post("/config/structured", json=_payload(form_channels))
    assert resp.status_code == 200, resp.text
    stored = {c["name"]: c for c in saved[0]["notifications"]["channels"]}
    assert stored["ha"]["allow_internal"] is True
    assert stored["lan-ntfy"]["allow_internal"] is True
    assert stored["relay"]["allow_internal"] is True
    assert stored["relay"]["smtp_insecure_plaintext"] is True
    assert "smtp_ca_file" not in stored["relay"]
    assert stored["private-ca"]["smtp_ca_file"] == "/config/certs/smtp-ca.pem"
    assert stored["private-ca"]["smtp_insecure_plaintext"] is False


def test_structured_save_is_strict_about_omitted_lan_opt_in(client, saved, monkeypatch):
    """A payload without allow_internal means off, even when the stored
    channel had it on: the save is refused and names channel and field
    (the URL path, which may carry a token, is never echoed)."""
    _stored(monkeypatch, [{
        "name": "ha", "type": "webhook", "enabled": True,
        "url": "http://192.168.1.50:8123/api/webhook/pond", "allow_internal": True,
    }])
    resp = client.post("/config/structured", json=_payload([
        {"name": "ha", "type": "webhook", "enabled": True,
         "url": "http://192.168.1.50:8123/api/webhook/pond", "method": "POST"},
    ]))
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert "'ha'" in body["error"] and "allow_internal" in body["error"]
    assert "/api/webhook/pond" not in body["error"]
    assert saved == []


def test_structured_save_refuses_stale_form_that_omits_a_stored_opt_in(
    client, saved, monkeypatch,
):
    """A cached pre-FDY-0571 config.js does not send allow_internal. For a
    hostname destination the static check cannot see the LAN address, so
    dropping the stored opt-in would silently break sends - refuse instead."""
    _stored(monkeypatch, [{
        "name": "ha", "type": "webhook", "enabled": True,
        "url": "http://homeassistant.local:8123/api/webhook/pond", "allow_internal": True,
    }])
    resp = client.post("/config/structured", json=_payload([
        {"name": "ha", "type": "webhook", "enabled": True,
         "url": "http://homeassistant.local:8123/api/webhook/pond", "method": "POST"},
    ]))
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert "allow_internal was not sent" in error and "reload" in error
    assert "/api/webhook/pond" not in error
    assert saved == []


def test_structured_save_rename_keeps_form_opt_in(client, saved, monkeypatch):
    _stored(monkeypatch, [{
        "name": "ha", "type": "webhook", "enabled": True,
        "url": "http://192.168.1.50/x", "allow_internal": True,
    }])
    resp = client.post("/config/structured", json=_payload([
        {"name": "home", "prev_name": "ha", "type": "webhook", "enabled": True,
         "url": "http://192.168.1.50/x", "method": "POST", "allow_internal": True},
    ]))
    assert resp.status_code == 200, resp.text
    [home] = saved[0]["notifications"]["channels"]
    assert home["name"] == "home" and home["allow_internal"] is True
    assert "prev_name" not in home


def test_structured_save_drops_hand_edited_discord_opt_in(client, saved, monkeypatch):
    _stored(monkeypatch, [{"name": "d", "type": "discord", "enabled": True,
                           "webhook_url": "https://discord.com/api/webhooks/1/x",
                           "allow_internal": True}])
    resp = client.post("/config/structured", json=_payload([
        {"name": "d", "type": "discord", "enabled": True, "webhook_url": "***REDACTED***"},
    ]))
    assert resp.status_code == 200, resp.text
    assert "allow_internal" not in saved[0]["notifications"]["channels"][0]


def test_structured_save_refuses_port_zero(client, saved):
    resp = client.post("/config/structured", json=_payload([
        {"name": "h", "type": "webhook", "enabled": True, "url": "https://example.com:0/x",
         "allow_internal": False},
    ]))
    assert resp.status_code == 422
    assert "port must be an integer between 1 and 65535" in resp.json()["error"]


def test_structured_save_clears_stored_opt_ins_the_form_turns_off(client, saved, monkeypatch):
    _stored(monkeypatch, [{
        "name": "mail", "type": "email", "enabled": True, "smtp_host": "smtp.example.com",
        "smtp_port": 587, "to_addresses": ["a@example.com"], "allow_internal": True,
        "smtp_insecure_plaintext": True, "smtp_ca_file": "/config/certs/old-ca.pem",
    }])
    resp = client.post("/config/structured", json=_payload([
        {"name": "mail", "type": "email", "enabled": True, "smtp_host": "smtp.example.com",
         "smtp_port": 587, "to_addresses": ["a@example.com"], "allow_internal": False,
         "smtp_insecure_plaintext": False, "smtp_ca_file": ""},
    ]))
    assert resp.status_code == 200, resp.text
    mail = saved[0]["notifications"]["channels"][0]
    assert mail["allow_internal"] is False
    assert mail["smtp_insecure_plaintext"] is False
    assert "smtp_ca_file" not in mail


def test_structured_save_refuses_non_boolean_opt_in(client, saved):
    resp = client.post("/config/structured", json=_payload([
        {"name": "ha", "type": "webhook", "enabled": True, "url": "http://192.168.1.50/x",
         "allow_internal": "true"},
    ]))
    assert resp.status_code == 422
    assert "allow_internal must be true or false" in resp.json()["error"]
    assert saved == []


def test_structured_save_metadata_refused_even_with_stored_opt_in(client, saved, monkeypatch):
    _stored(monkeypatch, [{"name": "ha", "type": "webhook", "enabled": True,
                           "url": "http://192.168.1.50/x", "allow_internal": True}])
    resp = client.post("/config/structured", json=_payload([
        {"name": "ha", "type": "webhook", "enabled": True, "url": "http://169.254.169.254/x"},
    ]))
    assert resp.status_code == 422
    assert saved == []


def test_structured_save_explicit_false_opt_in_refuses_lan(client, saved):
    resp = client.post("/config/structured", json=_payload([
        {"name": "ha", "type": "webhook", "enabled": True, "url": "http://192.168.1.50/x",
         "allow_internal": False},
    ]))
    assert resp.status_code == 422


def test_disabled_channel_not_validated(client, saved):
    resp = client.post("/config/structured", json=_payload([
        {"name": "old", "type": "webhook", "enabled": False, "url": "http://127.0.0.1/x"},
    ]))
    assert resp.status_code == 200


# ── Whole-document paths (raw YAML editor, restore) ─────────────────────────


def test_raw_yaml_lan_webhook_needs_explicit_opt_in(client, saved):
    doc = _payload([{"name": "ha", "type": "webhook", "enabled": True,
                     "url": "http://192.168.1.50:8123/api/webhook/SECRET-TOKEN"}])
    resp = client.post("/config", data={"raw_yaml": yaml.safe_dump(doc)})
    assert resp.status_code == 200
    assert saved == []
    alert = _error_alert(resp.text)
    assert "allow_internal" in alert
    assert "SECRET-TOKEN" not in alert

    doc["notifications"]["channels"][0]["allow_internal"] = True
    resp = client.post("/config", data={"raw_yaml": yaml.safe_dump(doc)})
    assert len(saved) == 1


def test_raw_yaml_error_does_not_echo_discord_secret(client, saved):
    doc = _payload([{"name": "d", "type": "discord", "enabled": True,
                     "webhook_url": "http://127.0.0.1/api/webhooks/1/TOPSECRET"}])
    resp = client.post("/config", data={"raw_yaml": yaml.safe_dump(doc)})
    assert saved == []
    alert = _error_alert(resp.text)
    assert "webhook_url" in alert
    assert "TOPSECRET" not in alert


def test_full_document_flag_types_and_ca_path():
    errors = validate_full_config(_payload([
        {"name": "m", "type": "email", "smtp_host": "smtp.example.com", "smtp_port": 2525,
         "to_addresses": ["a@example.com"], "smtp_insecure_plaintext": "yes",
         "smtp_ca_file": "relative/ca.pem"},
        {"name": "h", "type": "webhook", "url": "http://192.168.1.9/", "allow_internal": "true"},
    ]))
    joined = "\n".join(errors)
    assert "smtp_insecure_plaintext must be true or false" in joined
    assert "smtp_ca_file must be an absolute path" in joined
    assert "allow_internal must be true or false" in joined


def test_full_document_valid_lan_config_passes():
    assert validate_full_config(_payload([
        {"name": "m", "type": "email", "smtp_host": "relay.lan", "smtp_port": 2525,
         "to_addresses": ["a@example.com"], "allow_internal": True,
         "smtp_ca_file": "/config/certs/smtp-ca.pem"},
        {"name": "n", "type": "ntfy", "topic": "t", "server": "http://10.0.0.20",
         "allow_internal": True},
    ])) == []


# ── UI visibility ───────────────────────────────────────────────────────────


def test_config_page_shows_destination_security(client, monkeypatch):
    _stored(monkeypatch, [
        {"name": "relay", "type": "email", "enabled": True, "smtp_host": "192.168.1.25",
         "smtp_port": 25, "to_addresses": ["a@example.com"], "allow_internal": True,
         "smtp_insecure_plaintext": True},
        {"name": "gmail", "type": "email", "enabled": True, "smtp_host": "smtp.gmail.com",
         "smtp_port": 587, "to_addresses": ["a@example.com"],
         "smtp_ca_file": "/config/certs/smtp-ca.pem"},
        {"name": "phone", "type": "ntfy", "enabled": True, "topic": "t",
         "server": "https://ntfy.sh"},
    ])
    resp = client.get("/config")
    assert resp.status_code == 200
    html = resp.text
    table = html.split('id="channel-security"')[1].split("</table>")[0]
    relay = table.split('data-channel="relay"')[1].split("</tr>")[0]
    assert "INSECURE: plaintext SMTP" in relay
    assert "allowed" in relay
    gmail = table.split('data-channel="gmail"')[1].split("</tr>")[0]
    assert "STARTTLS required, verified" in gmail
    assert "/config/certs/smtp-ca.pem" in gmail
    phone = table.split('data-channel="phone"')[1].split("</tr>")[0]
    assert "refused" in phone
    assert 'id="channel-destination-policy"' in html


def test_config_page_names_channels_the_notifier_will_disable(client, monkeypatch):
    """A stored config the notifier would refuse still renders (no fallback
    to an empty notifications section) and the table names the problem."""
    _stored(monkeypatch, [
        {"name": "ha", "type": "webhook", "enabled": True, "url": "http://192.168.1.50/x"},
        {"name": "meta", "type": "webhook", "enabled": True,
         "url": "http://169.254.169.254/x", "allow_internal": True},
    ])
    resp = client.get("/config")
    assert resp.status_code == 200
    table = resp.text.split('id="channel-security"')[1].split("</table>")[0]
    ha = table.split('data-channel="ha"')[1].split("</tr>")[0]
    assert "Notifier will disable this channel" in ha
    assert "allow_internal" in ha
    meta = table.split('data-channel="meta"')[1].split("</tr>")[0]
    assert "link-local" in meta


def test_numeric_string_smtp_port_still_accepted():
    assert validate_full_config(_payload([
        {"name": "m", "type": "email", "smtp_host": "smtp.example.com", "smtp_port": "587",
         "to_addresses": ["a@example.com"]},
    ])) == []


def test_unnamed_channel_problem_is_shown(client, monkeypatch):
    _stored(monkeypatch, [{"type": "webhook", "enabled": True, "url": "http://127.0.0.1/x"}])
    table = client.get("/config").text.split('id="channel-security"')[1].split("</table>")[0]
    assert "Notifier will disable this channel" in table
