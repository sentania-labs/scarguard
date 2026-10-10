"""FDY-0569 regression tests: SG-17 (config restore), SG-24 (TLS values and
Caddy reloads), SG-30 (config_api switch into the 501 scaffold).

Everything here runs the real code: the FastAPI routes through TestClient
with the real config_store, secret_box and ConfigBackupManager pointed at a
temporary directory, and the real config/caddy_config.py both in-process and
as the subprocess the Caddy entrypoint runs. Only the ``caddy`` binary is a
stub (it is not available outside the caddy image); the stub records each
call and exits with the status the test asks for.
"""

from __future__ import annotations

import copy
import importlib.util
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

TEST_FILE = Path(__file__).resolve()
REPO_ROOT = next(
    (parent for parent in TEST_FILE.parents if (parent / "config" / "caddy_config.py").is_file()),
    None,
)

# Dummy, obviously fake secret material - never real credentials.
FAKE_SECRET = "placeholder-value-for-tests"
FAKE_WEBHOOK = "https://hooks.example.invalid/placeholder"

BASE_CFG: dict[str, Any] = {
    "system": {
        "armed": True,
        "log_level": "info",
        "timezone": "UTC",
        "auth": {"enabled": False},
    },
    "cameras": [{"name": "pond-north", "rtsp_url": "rtsp://localhost/test"}],
    "detection": {"model_path": "/models/best.pt", "confidence_threshold": 0.25},
    "redis": {"host": "localhost", "port": 6379},
    "notifications": {"channels": []},
    "tls": {"mode": "off", "domain": ""},
}

# Refused in every tls mode: Caddyfile syntax, whitespace, control characters.
INJECTION_DOMAINS = [
    "evil.example.com\n}\n:9999 {\n\trespond 200",
    "evil.example.com {",
    "evil.example.com import scarguard",
    "pond.example.com\r\nreverse_proxy attacker:80",
    "pond.example.com\x00",
    '"quoted.example.com"',
    "*.example.com",
    "-bad.example.com",
    "pond.example.com:99999",
]
# Fine for feedback links (manual/off), refused as an ACME site address (auto).
NOT_CERTIFICATE_DOMAINS = ["192.168.1.10", "localhost", "pond.lan:8443"]
MALICIOUS_DOMAINS = INJECTION_DOMAINS + NOT_CERTIFICATE_DOMAINS

MALICIOUS_CERT_PATHS = [
    "/config/certs/cert.pem\n\timport evil",
    "/config/certs/cert.pem\n",
    "/config/certs/../../etc/shadow",
    "/config/../etc/shadow",
    "/etc/ssl/private/key.pem",
    "/config/certs/cert pem",
    "/config/certs/{cert}.pem",
    "relative/cert.pem",
]


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Real config_store/secret_box/backup manager rooted in tmp_path."""
    import config_backup
    import config_store
    import main
    import secret_box

    config_path = tmp_path / "scarguard.yml"
    backup_dir = tmp_path / "backups"
    key_path = tmp_path / "secret_key"
    config_path.write_text(yaml.safe_dump(BASE_CFG, sort_keys=False))

    monkeypatch.setattr(config_store, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config_store, "_cache_cfg", None)
    monkeypatch.setattr(config_store, "_cache_mtime_ns", None)
    monkeypatch.setattr(config_backup, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(secret_box, "DEFAULT_KEY_PATH", str(key_path))
    secret_box.write_key_if_missing(str(key_path))

    manager = config_backup.ConfigBackupManager()
    monkeypatch.setattr(main, "backup_manager", manager)
    monkeypatch.setattr("db.get_latest_snapshots_by_camera", lambda: {})
    return {
        "config_path": config_path,
        "backup_dir": backup_dir,
        "key_path": key_path,
        "manager": manager,
        "tmp": tmp_path,
    }


@pytest.fixture()
def http(env: dict[str, Any]):
    from fastapi.testclient import TestClient
    from main import app

    c = TestClient(app)
    c.get("/login")
    c.headers["X-CSRF-Token"] = c.cookies.get("csrf_token", "")
    return c


def _write_backup(env: dict[str, Any], name: str, content: str) -> str:
    env["backup_dir"].mkdir(parents=True, exist_ok=True)
    (env["backup_dir"] / name).write_text(content)
    return name


def _cfg_with(**sections: Any) -> dict[str, Any]:
    cfg = copy.deepcopy(BASE_CFG)
    cfg.update(sections)
    return cfg


def _backups(env: dict[str, Any]) -> list[Path]:
    return sorted(env["backup_dir"].glob("scarguard_*.yml"))


def _leftover_tmp(env: dict[str, Any]) -> list[Path]:
    return [
        p for p in list(env["tmp"].iterdir()) + list(env["backup_dir"].iterdir())
        if p.name.endswith(".tmp")
    ]


# ── SG-17: config restore ────────────────────────────────────────────────────


def test_restore_refuses_malformed_yaml(env, http):
    before = env["config_path"].read_bytes()
    name = _write_backup(env, "scarguard_20260101T000000Z_manual.yml", "system: [unclosed\n  :")
    r = http.post(f"/admin/backups/{name}/restore")
    assert r.status_code == 422
    assert r.json()["ok"] is False
    assert "not valid YAML" in r.json()["error"]
    assert env["config_path"].read_bytes() == before
    # Refused before the pre-restore backup step: nothing new on disk.
    assert [p.name for p in _backups(env)] == [name]


@pytest.mark.parametrize("content", ["- just\n- a list\n", "42\n"])
def test_restore_refuses_non_mapping(env, http, content):
    before = env["config_path"].read_bytes()
    name = _write_backup(env, "scarguard_20260101T000000Z_manual.yml", content)
    r = http.post(f"/admin/backups/{name}/restore")
    assert r.status_code == 422
    assert env["config_path"].read_bytes() == before


def test_restore_refuses_schema_invalid_backup(env, http):
    before = env["config_path"].read_bytes()
    bad = _cfg_with(
        tls={"mode": "auto", "domain": MALICIOUS_DOMAINS[0]},
        cameras=[{"name": "", "rtsp_url": "http://not-rtsp"}],
    )
    bad["system"]["timezone"] = "Mars/Olympus_Mons"
    bad["deterrent"] = {"defaults": {"pre_delay_range": [300, 300]}}
    name = _write_backup(env, "scarguard_20260101T000000Z_manual.yml", yaml.safe_dump(bad))
    r = http.post(f"/admin/backups/{name}/restore")
    assert r.status_code == 422
    error = r.json()["error"]
    assert "tls.domain" in error
    assert "system.timezone" in error
    assert "pre_delay_range" in error
    # The rejected value itself is never echoed back.
    assert "respond 200" not in error
    assert env["config_path"].read_bytes() == before
    assert len(_backups(env)) == 1


@pytest.mark.parametrize("write_path", ["raw", "restore"])
def test_full_config_writes_reject_quoted_boolean(env, http, write_path):
    """Known schema fields must not be accepted through coercion."""
    before = env["config_path"].read_bytes()
    bad = _cfg_with(system={**BASE_CFG["system"], "armed": "false"})
    doc = yaml.safe_dump(bad)
    if write_path == "raw":
        r = http.post("/config", data={"raw_yaml": doc})
        assert "Config not saved" in r.text
    else:
        name = _write_backup(env, "scarguard_20260101T000000Z_manual.yml", doc)
        r = http.post(f"/admin/backups/{name}/restore")
        assert r.status_code == 422
    assert env["config_path"].read_bytes() == before


def test_restore_encrypts_plaintext_secrets_with_existing_key(env, http):
    import secret_box

    old = env["config_path"].read_bytes()
    incoming = _cfg_with(
        deterrent={"enabled": False, "tuya": {"api_key": FAKE_SECRET, "api_secret": FAKE_SECRET}},
        notifications={"channels": [{"name": "d", "type": "discord", "webhook_url": FAKE_WEBHOOK}]},
    )
    name = _write_backup(env, "scarguard_20260101T000000Z_manual.yml", yaml.safe_dump(incoming))
    r = http.post(f"/admin/backups/{name}/restore")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True

    on_disk_text = env["config_path"].read_text()
    assert FAKE_SECRET not in on_disk_text
    assert FAKE_WEBHOOK not in on_disk_text
    on_disk = yaml.safe_load(on_disk_text)
    assert secret_box.is_encrypted(on_disk["deterrent"]["tuya"]["api_secret"])
    assert secret_box.is_encrypted(on_disk["notifications"]["channels"][0]["webhook_url"])
    key = secret_box.load_key(str(env["key_path"]))
    assert secret_box.decrypt(on_disk["deterrent"]["tuya"]["api_secret"], key) == FAKE_SECRET

    # Recoverable last-good state: the replaced config is a backup.
    pre = env["backup_dir"] / body["pre_restore_backup"]
    assert "pre-restore" in pre.name
    assert pre.read_bytes() == old


def test_restore_refuses_plaintext_secrets_without_key(env, http):
    env["key_path"].unlink()
    before = env["config_path"].read_bytes()
    incoming = _cfg_with(deterrent={"tuya": {"api_secret": FAKE_SECRET}})
    name = _write_backup(env, "scarguard_20260101T000000Z_manual.yml", yaml.safe_dump(incoming))
    r = http.post(f"/admin/backups/{name}/restore")
    assert r.status_code == 422
    assert "secret key is unavailable" in r.json()["error"]
    assert env["config_path"].read_bytes() == before
    assert FAKE_SECRET not in env["config_path"].read_text()


def test_restore_refuses_secrets_encrypted_under_another_key(env, http):
    import secret_box

    other_key = secret_box.generate_key()
    before = env["config_path"].read_bytes()
    incoming = _cfg_with(
        deterrent={"tuya": {"api_secret": secret_box.encrypt(FAKE_SECRET, other_key)}},
    )
    name = _write_backup(env, "scarguard_20260101T000000Z_manual.yml", yaml.safe_dump(incoming))
    r = http.post(f"/admin/backups/{name}/restore")
    assert r.status_code == 422
    assert "do not decrypt" in r.json()["error"]
    assert env["config_path"].read_bytes() == before


def test_restore_interrupted_write_keeps_last_good(env, http, monkeypatch):
    """A crash part-way through writing leaves the live config untouched."""
    import config_store

    before = env["config_path"].read_bytes()
    incoming = _cfg_with(system={**BASE_CFG["system"], "armed": False})
    name = _write_backup(env, "scarguard_20260101T000000Z_manual.yml", yaml.safe_dump(incoming))

    real_dump = yaml.dump

    def interrupted_dump(data: Any, stream: Any = None, **kw: Any) -> Any:
        text = real_dump(data, **kw)
        stream.write(text[: len(text) // 2])
        stream.flush()
        raise OSError("simulated power loss mid-write")

    monkeypatch.setattr(config_store.yaml, "dump", interrupted_dump)
    r = http.post(f"/admin/backups/{name}/restore")
    monkeypatch.setattr(config_store.yaml, "dump", real_dump)

    assert r.status_code == 500
    assert "current config was kept" in r.json()["error"]
    assert env["config_path"].read_bytes() == before
    assert _leftover_tmp(env) == []
    pre = [p for p in _backups(env) if "pre-restore" in p.name]
    assert len(pre) == 1 and pre[0].read_bytes() == before


def test_restore_refused_when_pre_restore_backup_fails(env, http, monkeypatch):
    before = env["config_path"].read_bytes()
    name = _write_backup(
        env, "scarguard_20260101T000000Z_manual.yml", yaml.safe_dump(_cfg_with()),
    )
    monkeypatch.setattr(env["manager"], "_create_backup", lambda reason: None)
    r = http.post(f"/admin/backups/{name}/restore")
    assert r.status_code == 500
    assert "nothing was changed" in r.json()["error"]
    assert env["config_path"].read_bytes() == before


def test_backup_names_are_unique_within_one_second(env, monkeypatch):
    import config_backup

    frozen = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return frozen

    monkeypatch.setattr(config_backup, "datetime", FrozenDatetime)
    contents = []
    names = []
    for armed in (True, False, True):
        cfg = _cfg_with(system={**BASE_CFG["system"], "armed": armed})
        env["config_path"].write_text(yaml.safe_dump(cfg))
        contents.append(env["config_path"].read_bytes())
        names.append(env["manager"].create_backup("manual"))
    assert None not in names
    assert len(set(names)) == 3
    for name, content in zip(names, contents):
        assert (env["backup_dir"] / name).read_bytes() == content
    assert _leftover_tmp(env) == []


def test_restore_valid_backup_is_applied(env, http):
    """Healthy flow: a valid backup restores and the config page still renders."""
    incoming = _cfg_with(
        system={**BASE_CFG["system"], "armed": False, "timezone": "America/Chicago"},
        tls={"mode": "auto", "domain": "pond.example.com"},
    )
    name = _write_backup(env, "scarguard_20260101T000000Z_manual.yml", yaml.safe_dump(incoming))
    r = http.post(f"/admin/backups/{name}/restore")
    assert r.status_code == 200, r.text
    on_disk = yaml.safe_load(env["config_path"].read_text())
    assert on_disk["system"]["armed"] is False
    assert on_disk["tls"] == {"mode": "auto", "domain": "pond.example.com"}
    assert http.get("/config").status_code == 200
    assert http.get("/admin/backups").status_code == 200


# ── SG-24: TLS values on the web write paths ─────────────────────────────────


def _structured(tls: dict[str, Any], **system: Any) -> dict[str, Any]:
    return {
        "system": {"armed": True, "timezone": "UTC", "auth": {"enabled": False}, **system},
        "cameras": [{"name": "pond-north", "rtsp_url": "***REDACTED***"}],
        "tls": tls,
    }


# The structured route answers every validation failure with the same scrubbed
# message (issue #95), so each case below differs from an accepted payload
# (test_structured_save_valid_tls_still_works) only in the one tls value.


@pytest.mark.parametrize("domain", INJECTION_DOMAINS)
def test_structured_save_rejects_malicious_domain(env, http, domain):
    before = env["config_path"].read_bytes()
    for mode in ("auto", "manual", "off"):
        r = http.post("/config/structured", json=_structured({"mode": mode, "domain": domain}))
        assert r.status_code == 422
        assert r.json()["error"] == "Invalid config payload"
    assert env["config_path"].read_bytes() == before


@pytest.mark.parametrize("domain", NOT_CERTIFICATE_DOMAINS)
def test_structured_save_lan_domain_only_outside_auto(env, http, domain):
    before = env["config_path"].read_bytes()
    r = http.post("/config/structured", json=_structured({"mode": "auto", "domain": domain}))
    assert r.status_code == 422
    assert env["config_path"].read_bytes() == before
    r = http.post("/config/structured", json=_structured({"mode": "manual", "domain": domain}))
    assert r.status_code == 200, r.text
    assert yaml.safe_load(env["config_path"].read_text())["tls"]["domain"] == domain


def test_structured_save_keeps_invalid_stored_tls_instead_of_wiping_it(env, http):
    """A tls section that no longer validates renders as form defaults; an
    unrelated save must not turn those defaults into 'HTTPS off' on disk."""
    stored_tls = {"mode": "auto", "domain": "192.168.1.10"}
    env["config_path"].write_text(yaml.safe_dump(_cfg_with(tls=stored_tls)))
    page = http.get("/config")
    assert page.status_code == 200
    assert '"tlsFallback": true' in page.text
    payload = _structured(
        {"mode": "off", "domain": "", "cert_path": "/config/certs/cert.pem",
         "key_path": "/config/certs/key.pem"},
        armed=False,
    )
    payload["tls_unchanged"] = True
    r = http.post("/config/structured", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tls_changed"] is False
    assert any("TLS settings are invalid" in w for w in body["warnings"])
    on_disk = yaml.safe_load(env["config_path"].read_text())
    assert on_disk["tls"] == stored_tls
    assert on_disk["system"]["armed"] is False


def test_structured_save_can_replace_invalid_tls_with_defaults(env, http):
    stored_tls = {"mode": "auto", "domain": "192.168.1.10"}
    env["config_path"].write_text(yaml.safe_dump(_cfg_with(tls=stored_tls)))
    r = http.post(
        "/config/structured",
        json=_structured({
            "mode": "off",
            "domain": "",
            "cert_path": "/config/certs/cert.pem",
            "key_path": "/config/certs/key.pem",
        }),
    )
    assert r.status_code == 200, r.text
    assert r.json()["tls_changed"] is True
    assert yaml.safe_load(env["config_path"].read_text())["tls"] == {
        "mode": "off",
        "domain": "",
        "cert_path": "/config/certs/cert.pem",
        "key_path": "/config/certs/key.pem",
    }


@pytest.mark.parametrize("path", MALICIOUS_CERT_PATHS)
def test_structured_save_rejects_bad_cert_paths(env, http, path):
    before = env["config_path"].read_bytes()
    for field in ("cert_path", "key_path"):
        r = http.post("/config/structured", json=_structured({"mode": "manual", field: path}))
        assert r.status_code == 422
    assert env["config_path"].read_bytes() == before


def test_structured_save_auto_mode_requires_domain(env, http):
    before = env["config_path"].read_bytes()
    r = http.post("/config/structured", json=_structured({"mode": "auto", "domain": ""}))
    assert r.status_code == 422
    assert env["config_path"].read_bytes() == before


def test_structured_save_valid_tls_still_works(env, http):
    r = http.post(
        "/config/structured",
        json=_structured({"mode": "auto", "domain": "Pond.Example.com"}),
    )
    assert r.status_code == 200, r.text
    assert r.json()["tls_changed"] is True
    on_disk = yaml.safe_load(env["config_path"].read_text())
    assert on_disk["tls"]["domain"] == "pond.example.com"
    # The redacted camera URL placeholder kept the stored secret.
    assert on_disk["cameras"][0]["rtsp_url"] == "rtsp://localhost/test"

    # A later save carries the revision the previous response returned, as
    # the config form does (FDY-0566 optimistic concurrency).
    revision = r.json()["revision"]
    r = http.post(
        "/config/structured",
        json=_structured({
            "mode": "manual",
            "domain": "pond.example.com",
            "cert_path": "/config/certs/fullchain.pem",
            "key_path": "/config/tls/privkey.pem",
        }, revision=revision),
    )
    assert r.status_code == 200, r.text


def test_raw_yaml_save_rejects_malicious_tls(env, http):
    before = env["config_path"].read_bytes()
    bad = _cfg_with(tls={"mode": "auto", "domain": MALICIOUS_DOMAINS[0]})
    r = http.post("/config", data={"raw_yaml": yaml.safe_dump(bad)})
    assert r.status_code == 200
    assert "Config not saved" in r.text
    assert "tls.domain" in r.text
    assert env["config_path"].read_bytes() == before


def test_raw_yaml_save_rejects_malformed_yaml(env, http):
    before = env["config_path"].read_bytes()
    r = http.post("/config", data={"raw_yaml": "system: [unclosed\n  :"})
    assert r.status_code == 200
    assert "could not be parsed" in r.text
    assert env["config_path"].read_bytes() == before


def test_raw_yaml_save_valid_still_works(env, http):
    good = _cfg_with(system={**BASE_CFG["system"], "armed": False})
    r = http.post("/config", data={"raw_yaml": yaml.safe_dump(good)})
    assert r.status_code == 200
    assert "Config not saved" not in r.text
    assert yaml.safe_load(env["config_path"].read_text())["system"]["armed"] is False


# ── SG-24: the Caddy generator artifact ──────────────────────────────────────


def _load_caddy_config():
    if REPO_ROOT is None:
        pytest.skip("repository Caddy artifacts are not included in the web runtime image")
    caddy_config_py = REPO_ROOT / "config" / "caddy_config.py"
    spec = importlib.util.spec_from_file_location("caddy_config_under_test", caddy_config_py)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("domain", MALICIOUS_DOMAINS)
def test_caddy_render_refuses_malicious_domain(domain):
    cc = _load_caddy_config()
    with pytest.raises(cc.RenderError):
        cc.render({"tls": {"mode": "auto", "domain": domain}})
    if domain in INJECTION_DOMAINS:
        with pytest.raises(cc.RenderError):
            cc.render({"tls": {"mode": "manual", "domain": domain}}, file_exists=lambda _p: True)


@pytest.mark.parametrize("path", MALICIOUS_CERT_PATHS)
def test_caddy_render_refuses_bad_cert_paths(path):
    cc = _load_caddy_config()
    for field in ("cert_path", "key_path"):
        with pytest.raises(cc.RenderError):
            cc.render({"tls": {"mode": "manual", field: path}}, file_exists=lambda _p: True)


def test_caddy_render_constrained_output():
    cc = _load_caddy_config()
    auto = cc.render({"tls": {"mode": "auto", "domain": "pond.example.com"}})
    assert "\npond.example.com {\n\timport scarguard\n}\n" in auto
    manual = cc.render(
        {"tls": {"mode": "manual"}}, https_port="8443", file_exists=lambda _p: True,
    )
    assert "\ttls /config/certs/cert.pem /config/certs/key.pem\n" in manual
    assert "redir https://{host}:8443{uri} permanent" in manual
    assert cc.render({}) == cc.render_http_only()
    assert cc.render({"tls": {"mode": False}}) == cc.render_http_only()


def _stub_caddy(tmp_path: Path, log: Path) -> Path:
    stub = tmp_path / "caddy"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$1 $3" >> "{log}"\n'
        'case "$1" in\n'
        '  validate) exit "${CADDY_STUB_VALIDATE_EXIT:-0}" ;;\n'
        '  reload) exit "${CADDY_STUB_RELOAD_EXIT:-0}" ;;\n'
        "esac\n"
        "exit 64\n",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub


def _run_caddy_config(
    tmp_path: Path, action: str, cfg_text: str, *, validate_exit: int = 0, reload_exit: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path, list[str]]:
    if REPO_ROOT is None:
        pytest.skip("repository Caddy artifacts are not included in the web runtime image")
    caddy_config_py = REPO_ROOT / "config" / "caddy_config.py"
    shared_dir = REPO_ROOT / "shared"
    config = tmp_path / "scarguard.yml"
    config.write_text(cfg_text)
    caddyfile = tmp_path / "Caddyfile"
    log = tmp_path / "caddy-calls.log"
    log.write_text("")
    env = {
        **os.environ,
        "CADDY_BIN": str(_stub_caddy(tmp_path, log)),
        "CADDY_STUB_VALIDATE_EXIT": str(validate_exit),
        "CADDY_STUB_RELOAD_EXIT": str(reload_exit),
        "PYTHONPATH": os.pathsep.join([str(shared_dir), os.environ.get("PYTHONPATH", "")]),
        "HTTPS_PORT": "443",
    }
    proc = subprocess.run(
        [sys.executable, str(caddy_config_py), action, str(config), str(caddyfile)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    return proc, caddyfile, log.read_text().split("\n")[:-1]


def _seed_running(tmp_path: Path) -> str:
    cc = _load_caddy_config()
    current = cc.render({"tls": {"mode": "auto", "domain": "pond.example.com"}})
    (tmp_path / "Caddyfile").write_text(current)
    return current


def test_caddy_reload_refuses_malicious_config_without_touching_caddy(tmp_path):
    current = _seed_running(tmp_path)
    cfg = yaml.safe_dump({"tls": {"mode": "auto", "domain": MALICIOUS_DOMAINS[0]}})
    proc, caddyfile, calls = _run_caddy_config(tmp_path, "reload", cfg)
    assert proc.returncode == 1
    assert "keeping the current Caddy config" in proc.stderr
    assert caddyfile.read_text() == current
    assert calls == []


def test_caddy_reload_refuses_malformed_config(tmp_path):
    current = _seed_running(tmp_path)
    proc, caddyfile, calls = _run_caddy_config(tmp_path, "reload", "tls: [unclosed\n  :")
    assert proc.returncode == 1
    assert caddyfile.read_text() == current
    assert calls == []


def test_caddy_reload_failed_validation_keeps_current(tmp_path):
    current = _seed_running(tmp_path)
    cfg = yaml.safe_dump({"tls": {"mode": "auto", "domain": "other.example.com"}})
    proc, caddyfile, calls = _run_caddy_config(tmp_path, "reload", cfg, validate_exit=1)
    assert proc.returncode == 1
    assert caddyfile.read_text() == current
    assert [c.split()[0] for c in calls] == ["validate"]
    # The validated file was a candidate, never the live Caddyfile.
    assert calls[0].split()[1] != str(caddyfile)
    assert not any(p.name.endswith(".candidate") for p in tmp_path.iterdir())


def test_caddy_reload_failure_restores_last_good(tmp_path):
    current = _seed_running(tmp_path)
    cfg = yaml.safe_dump({"tls": {"mode": "auto", "domain": "other.example.com"}})
    proc, caddyfile, calls = _run_caddy_config(tmp_path, "reload", cfg, reload_exit=1)
    assert proc.returncode == 1
    assert [c.split()[0] for c in calls] == ["validate", "reload"]
    assert caddyfile.read_text() == current
    assert (tmp_path / "Caddyfile.last-good").read_text() == current


def test_caddy_reload_valid_change_applies_atomically(tmp_path):
    current = _seed_running(tmp_path)
    cfg = yaml.safe_dump({"tls": {"mode": "auto", "domain": "other.example.com"}})
    proc, caddyfile, calls = _run_caddy_config(tmp_path, "reload", cfg)
    assert proc.returncode == 0, proc.stderr
    assert [c.split()[0] for c in calls] == ["validate", "reload"]
    assert "\nother.example.com {\n" in caddyfile.read_text()
    assert (tmp_path / "Caddyfile.last-good").read_text() == current


def test_caddy_generate_falls_back_to_http_on_bad_values(tmp_path):
    cfg = yaml.safe_dump({"tls": {"mode": "auto", "domain": MALICIOUS_DOMAINS[0]}})
    proc, caddyfile, _calls = _run_caddy_config(tmp_path, "generate", cfg)
    assert proc.returncode == 0
    assert caddyfile.read_text() == _load_caddy_config().render_http_only()
    assert "respond 200" not in caddyfile.read_text()


def test_caddy_generate_falls_back_when_caddy_validate_fails(tmp_path):
    cfg = yaml.safe_dump({"tls": {"mode": "auto", "domain": "pond.example.com"}})
    proc, caddyfile, calls = _run_caddy_config(tmp_path, "generate", cfg, validate_exit=1)
    assert proc.returncode == 0
    assert [c.split()[0] for c in calls] == ["validate"]
    assert caddyfile.read_text() == _load_caddy_config().render_http_only()


# ── SG-30: config_api.enabled must not route writes into the 501 scaffold ───


def test_caddy_ignores_config_api_enabled(tmp_path):
    cc = _load_caddy_config()
    cfg = {"system": {"config_api": {"enabled": True}}, "tls": {"mode": "off"}}
    rendered = cc.render(cfg)
    assert "config-api" not in rendered
    assert "\treverse_proxy web:8080\n" in rendered
    proc, caddyfile, _calls = _run_caddy_config(tmp_path, "generate", yaml.safe_dump(cfg))
    assert proc.returncode == 0
    assert "config-api" not in caddyfile.read_text()
    assert "Ignoring system.config_api.enabled" in proc.stderr


def test_structured_save_refuses_enabling_config_api(env, http):
    before = env["config_path"].read_bytes()
    r = http.post(
        "/config/structured",
        json=_structured({"mode": "off"}, config_api={"enabled": True}),
    )
    assert r.status_code == 422
    assert "config_api" in r.json()["error"]
    assert env["config_path"].read_bytes() == before


def test_settings_writes_keep_working_with_config_api_flag_on_disk(env, http):
    """A hand-set flag on disk must not block settings writes through web."""
    on_disk = _cfg_with(system={**BASE_CFG["system"], "config_api": {"enabled": True}})
    env["config_path"].write_text(yaml.safe_dump(on_disk))
    r = http.post("/config/structured", json=_structured({"mode": "off"}, armed=False))
    assert r.status_code == 200, r.text
    assert yaml.safe_load(env["config_path"].read_text())["system"]["armed"] is False


def test_raw_yaml_and_restore_refuse_config_api_enabled(env, http):
    before = env["config_path"].read_bytes()
    doc = yaml.safe_dump(
        _cfg_with(system={**BASE_CFG["system"], "config_api": {"enabled": True}}),
    )
    r = http.post("/config", data={"raw_yaml": doc})
    assert "Config not saved" in r.text
    assert "config_api" in r.text
    name = _write_backup(env, "scarguard_20260101T000000Z_manual.yml", doc)
    r = http.post(f"/admin/backups/{name}/restore")
    assert r.status_code == 422
    assert "config_api" in r.json()["error"]
    assert env["config_path"].read_bytes() == before
