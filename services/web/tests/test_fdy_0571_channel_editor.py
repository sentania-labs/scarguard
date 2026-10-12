"""FDY-0571: the channel editor (static/config.js) carries the destination
security settings - allow_internal (webhook, ntfy, email; off by default),
smtp_ca_file and smtp_insecure_plaintext (email) - and readChannels() sends
them, so the strict structured save sees exactly what the operator set.

Runs the real config.js under Node with a minimal DOM stand-in (no inline
script on the page: CSP is script-src 'self'). Skipped when Node is absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

CONFIG_JS = Path(__file__).resolve().parents[1] / "src" / "static" / "config.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# Loads config.js into a VM context whose document has no elements, so every
# page bootstrap finds nothing to wire. Each case builds a channel card with
# the real buildChannelCard(), turns the generated .ch-field markup into
# stand-in elements, applies the requested checkbox/text edits and returns
# what the real readChannels() would post.
_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const [jsPath, casesJson] = process.argv.slice(1);
const noop = () => {};
const classList = { add: noop, remove: noop, toggle: noop, contains: () => false };
let cards = [];
const document = {
  getElementById: () => null,
  querySelectorAll: (sel) => (sel === "#channels-list .camera-card" ? cards : []),
  querySelector: () => null,
  addEventListener: noop,
  createElement: () => ({ dataset: {}, className: "", innerHTML: "", classList }),
  body: { classList },
};
const ctx = {
  document, console, JSON, Array, String, parseInt, isNaN, Object,
  localStorage: { getItem: () => null, setItem: noop },
  location: { hash: "" }, history: { replaceState: noop },
  requestAnimationFrame: noop, MutationObserver: function () { this.observe = noop; },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(jsPath, "utf8"), ctx);

function attr(tag, name) {
  const m = tag.match(new RegExp(name + '="([^"]*)"'));
  return m ? m[1].replace(/&quot;/g, '"').replace(/&lt;/g, "<").replace(/&amp;/g, "&") : undefined;
}

function fieldsOf(html) {
  const out = [];
  const re = /<(input|select|textarea)\b([^>]*)>/g;
  let m;
  while ((m = re.exec(html))) {
    const [, tagName, rest] = m;
    if (!/class="ch-field"/.test(rest)) continue;
    const el = {
      tagName: tagName.toUpperCase(),
      type: tagName === "input" ? attr(rest, "type") : tagName,
      dataset: { field: attr(rest, "data-field") },
      checked: /\schecked\b/.test(rest),
      value: tagName === "input" ? (attr(rest, "value") || "") : "",
    };
    if (tagName === "select") {
      const body = html.slice(re.lastIndex, html.indexOf("</select>", re.lastIndex));
      const sel = body.match(/<option value="([^"]*)" selected>/) || body.match(/<option value="([^"]*)"/);
      el.value = sel ? sel[1] : "";
    }
    if (tagName === "textarea") {
      el.value = html.slice(re.lastIndex, html.indexOf("</textarea>", re.lastIndex));
    }
    out.push(el);
  }
  return out;
}

const results = JSON.parse(casesJson).map(({ channel, edits }) => {
  const card = vm.runInContext("buildChannelCard", ctx)(channel);
  const fields = fieldsOf(card.innerHTML);
  const initial = Object.fromEntries(fields.map(f => [f.dataset.field, f.type === "checkbox" ? f.checked : f.value]));
  for (const [field, value] of Object.entries(edits || {})) {
    const el = fields.find(f => f.dataset.field === field);
    if (el.type === "checkbox") el.checked = value; else el.value = value;
  }
  const name = { value: channel.name || "", dataset: { savedName: channel.name || "" } };
  const enabled = { checked: channel.enabled !== false };
  cards = [{
    dataset: card.dataset,
    querySelector: (sel) => (sel === ".ch-name" ? name : sel === ".ch-enabled" ? enabled : null),
    querySelectorAll: (sel) => (sel === ".ch-field" ? fields : []),
  }];
  const posted = vm.runInContext("readChannels", ctx)()[0];
  return { html: card.innerHTML, initial, posted };
});
process.stdout.write(JSON.stringify(results));
"""


def _run(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["node", "-e", _HARNESS, str(CONFIG_JS), json.dumps(cases)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("ch_type", ["webhook", "ntfy", "email"])
def test_new_channel_has_lan_opt_in_off_and_posts_false(ch_type: str) -> None:
    [result] = _run([{"channel": {"type": ch_type, "name": "c", "enabled": True}}])
    assert 'data-field="allow_internal"' in result["html"]
    assert result["initial"]["allow_internal"] is False
    assert result["posted"]["allow_internal"] is False


def test_discord_card_has_no_lan_opt_in() -> None:
    [result] = _run([{"channel": {"type": "discord", "name": "d", "enabled": True}}])
    assert "allow_internal" not in result["html"]
    assert "allow_internal" not in result["posted"]


def test_email_card_carries_ca_file_and_insecure_opt_in() -> None:
    new, edited = _run([
        {"channel": {"type": "email", "name": "m", "enabled": True}},
        {"channel": {"type": "email", "name": "relay", "enabled": True,
                     "smtp_host": "192.168.1.25", "smtp_port": 25},
         "edits": {"allow_internal": True, "smtp_insecure_plaintext": True,
                   "smtp_ca_file": " /config/certs/smtp-ca.pem "}},
    ])
    assert new["posted"]["smtp_insecure_plaintext"] is False
    assert new["posted"]["smtp_ca_file"] == ""
    assert "INSECURE" in new["html"]
    assert 'placeholder="/config/certs/smtp-ca.pem"' in new["html"]
    assert edited["posted"]["allow_internal"] is True
    assert edited["posted"]["smtp_insecure_plaintext"] is True
    assert edited["posted"]["smtp_ca_file"] == "/config/certs/smtp-ca.pem"
    assert edited["posted"]["smtp_port"] == 25


def test_stored_settings_round_trip_and_only_true_turns_opt_ins_on() -> None:
    stored, stringly = _run([
        {"channel": {"type": "email", "name": "relay", "enabled": True,
                     "allow_internal": True, "smtp_insecure_plaintext": True,
                     "smtp_ca_file": "/config/certs/smtp-ca.pem"}},
        {"channel": {"type": "ntfy", "name": "n", "enabled": True, "allow_internal": "yes"}},
    ])
    assert stored["initial"]["allow_internal"] is True
    assert stored["initial"]["smtp_insecure_plaintext"] is True
    assert stored["posted"]["smtp_ca_file"] == "/config/certs/smtp-ca.pem"
    assert stringly["initial"]["allow_internal"] is False
    assert stringly["posted"]["allow_internal"] is False


def test_unchanged_fields_still_post_as_before() -> None:
    [result] = _run([{"channel": {"type": "ntfy", "name": "n", "enabled": True,
                                  "server": "https://ntfy.sh", "topic": "pond",
                                  "priority": "4"}}])
    posted = result["posted"]
    assert posted["server"] == "https://ntfy.sh"
    assert posted["topic"] == "pond"
    assert posted["priority"] == "4"
    assert posted["include_snapshot"] is True
