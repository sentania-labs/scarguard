"""FDY-0572: the notifier's per-channel delivery bounds
(``notifications.delivery.queue_size`` / ``send_deadline_seconds``) live in
``scarguard.yml`` and are edited on the config page like every other setting.

Exercises the real config page, structured save, ``validate_full_config``
and, under Node, the real ``readForm()`` from static/config.js; only
config_store I/O is stubbed.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from config_model import validate_full_config

CONFIG_JS = Path(__file__).resolve().parents[1] / "src" / "static" / "config.js"

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
CHANNEL = {"name": "pond", "type": "ntfy", "topic": "t", "server": "https://ntfy.sh"}


def _stored(monkeypatch: pytest.MonkeyPatch, notifications: dict) -> list[dict]:
    cfg = {**copy.deepcopy(BASE), "notifications": notifications}
    monkeypatch.setattr("config_store.load", lambda: copy.deepcopy(cfg))
    monkeypatch.setattr("config_store.load_cached", lambda **_kw: copy.deepcopy(cfg))
    calls: list[dict] = []
    monkeypatch.setattr("config_store.save", lambda c: calls.append(copy.deepcopy(c)))
    return calls


def _input_value(html: str, element_id: str) -> str:
    tag = html.split(f'id="{element_id}"')[1].split(">")[0]
    return tag.split('value="')[1].split('"')[0]


def test_config_page_shows_stored_bounds(client, monkeypatch):
    _stored(monkeypatch, {"channels": [CHANNEL], "delivery": {"queue_size": 7, "send_deadline_seconds": 90}})
    html = client.get("/config").text
    section = html.split('id="section-notification-delivery"')[1]
    assert 'data-subtab="notifications"' in section.split(">")[0]
    assert _input_value(html, "notif-delivery-queue-size") == "7"
    assert _input_value(html, "notif-delivery-send-deadline") == "90"


def test_config_page_shows_defaults_when_unset(client, monkeypatch):
    _stored(monkeypatch, {"channels": [CHANNEL]})
    html = client.get("/config").text
    assert _input_value(html, "notif-delivery-queue-size") == "50"
    assert _input_value(html, "notif-delivery-send-deadline") == "60"


def test_structured_save_writes_bounds(client, monkeypatch):
    saved = _stored(monkeypatch, {"channels": [CHANNEL]})
    resp = client.post("/config/structured", json={
        **copy.deepcopy(BASE),
        "notifications": {"channels": [CHANNEL], "delivery": {"queue_size": 20, "send_deadline_seconds": 120}},
    })
    assert resp.status_code == 200, resp.text
    assert saved[-1]["notifications"]["delivery"] == {"queue_size": 20, "send_deadline_seconds": 120}
    assert saved[-1]["notifications"]["channels"][0]["name"] == "pond"


def test_structured_save_without_bounds_keeps_stored_values(client, monkeypatch):
    stored = {"queue_size": 9, "send_deadline_seconds": 15}
    saved = _stored(monkeypatch, {"channels": [CHANNEL], "delivery": dict(stored)})
    resp = client.post("/config/structured", json={**copy.deepcopy(BASE), "notifications": {"channels": [CHANNEL]}})
    assert resp.status_code == 200, resp.text
    assert saved[-1]["notifications"]["delivery"] == stored


def test_structured_save_partial_bounds_keep_the_other_stored_value(client, monkeypatch):
    saved = _stored(monkeypatch, {"channels": [CHANNEL], "delivery": {"queue_size": 9, "send_deadline_seconds": 15}})
    resp = client.post("/config/structured", json={
        **copy.deepcopy(BASE), "notifications": {"channels": [CHANNEL], "delivery": {"queue_size": 20}},
    })
    assert resp.status_code == 200, resp.text
    assert saved[-1]["notifications"]["delivery"] == {"queue_size": 20, "send_deadline_seconds": 15}


def test_bad_stored_bound_does_not_hide_channel_problems(client, monkeypatch):
    lan_hook = {"name": "lan-hook", "type": "webhook", "url": "http://192.168.1.50/hook", "enabled": True}
    _stored(monkeypatch, {"channels": [lan_hook], "delivery": {"queue_size": 0}})
    html = client.get("/config").text
    row = html.split('id="channel-security"')[1].split('data-channel="lan-hook"')[1].split("</tr>")[0]
    assert "Notifier will disable this channel" in row
    assert _input_value(html, "notif-delivery-queue-size") == "50"


@pytest.mark.parametrize("delivery", [
    {"queue_size": 0, "send_deadline_seconds": 60},
    {"queue_size": 1001, "send_deadline_seconds": 60},
    {"queue_size": 50, "send_deadline_seconds": 4},
    {"queue_size": 50, "send_deadline_seconds": 601},
])
def test_structured_save_refuses_out_of_range_bounds(client, monkeypatch, delivery):
    saved = _stored(monkeypatch, {"channels": [CHANNEL]})
    resp = client.post("/config/structured", json={
        **copy.deepcopy(BASE), "notifications": {"channels": [CHANNEL], "delivery": delivery},
    })
    assert resp.status_code == 422
    assert saved == []


def test_full_document_validation_checks_bounds():
    good = {**copy.deepcopy(BASE), "notifications": {"channels": [], "delivery": {"queue_size": 5, "send_deadline_seconds": 30}}}
    assert validate_full_config(good) == []
    bad = copy.deepcopy(good)
    bad["notifications"]["delivery"]["send_deadline_seconds"] = "30"
    assert any("notifications.delivery.send_deadline_seconds" in e for e in validate_full_config(bad))


# The page bootstrap runs against an empty document; readForm() then reads
# every field by id from stand-in elements whose value comes from VALUES
# (empty otherwise).
_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const [jsPath, valuesJson] = process.argv.slice(1);
const values = JSON.parse(valuesJson);
const noop = () => {};
const classList = { add: noop, remove: noop, toggle: noop, contains: () => false };
let loaded = false;
const document = {
  getElementById: (id) => (loaded
    ? { value: id in values ? values[id] : "", checked: false, dataset: {}, classList }
    : null),
  querySelectorAll: () => [],
  querySelector: () => null,
  addEventListener: noop,
  createElement: () => ({ dataset: {}, className: "", innerHTML: "", classList }),
  body: { classList },
};
const ctx = {
  document, console, JSON, Array, String, Number, parseInt, parseFloat, isNaN, Object, Math,
  localStorage: { getItem: () => null, setItem: noop },
  location: { hash: "" }, history: { replaceState: noop },
  requestAnimationFrame: noop, MutationObserver: function () { this.observe = noop; },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(jsPath, "utf8"), ctx);
loaded = true;
const data = vm.runInContext("readForm", ctx)();
const errors = vm.runInContext("validate", ctx)(data);
process.stdout.write(JSON.stringify({ delivery: data.notifications.delivery, errors }));
"""


def _read_form(values: dict[str, str]) -> dict:
    proc = subprocess.run(
        ["node", "-e", _HARNESS, str(CONFIG_JS), json.dumps(values)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_form_posts_bounds_and_flags_out_of_range():
    base = {"conf-slider": "0.5", "det-model-path": "/models/best.pt", "training-val-split": "0.15",
            "training-workers": "2"}
    ok = _read_form({**base, "notif-delivery-queue-size": "12", "notif-delivery-send-deadline": "45"})
    assert ok["delivery"] == {"queue_size": 12, "send_deadline_seconds": 45}
    assert not any("Notification delivery" in e for e in ok["errors"])
    empty = _read_form(base)
    assert empty["delivery"] == {"queue_size": 50, "send_deadline_seconds": 60}
    bad = _read_form({**base, "notif-delivery-queue-size": "0", "notif-delivery-send-deadline": "900"})
    assert sum("Notification delivery" in e for e in bad["errors"]) == 2
