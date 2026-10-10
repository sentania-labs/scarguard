"""FDY-0570 regression: credentials stay out of logs, viewer pages and plaintext files.

SG-18 / SG-35. Every credential here is a random sentinel built at runtime;
the assertions only check that the sentinel is absent, never print it.
"""

from __future__ import annotations

import io
import logging
import os
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml


def _sentinel() -> str:
    return f"sentinel{uuid.uuid4().hex}"


@pytest.fixture()
def secrets() -> dict[str, str]:
    return {name: _sentinel() for name in ("rtsp", "roboflow", "hook", "ntfy")}


def _plaintext_cfg(s: dict[str, str]) -> dict[str, Any]:
    return {
        "system": {"armed": True, "log_level": "info", "auth": {"enabled": False}},
        "cameras": [
            {
                "name": "pond-north",
                "rtsp_url": f"rtsp://viewer:{s['rtsp']}@192.0.2.10:554/stream1",
                "enabled": True,
            }
        ],
        "training": {"sources": {"roboflow": {"api_key": s["roboflow"]}}},
        "notifications": {
            "channels": [
                {"name": "hook", "type": "webhook", "url": "https://hooks.example.com/x",
                 "auth_token": s["hook"]},
                {"name": "push", "type": "ntfy", "topic": "pond", "token": s["ntfy"]},
            ]
        },
    }


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Real config_store/secret_box rooted in tmp_path."""
    import config_store
    import secret_box

    config_path = tmp_path / "scarguard.yml"
    key_path = tmp_path / "secret_key"
    monkeypatch.setattr(config_store, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config_store, "_cache_cfg", None)
    monkeypatch.setattr(config_store, "_cache_mtime_ns", None)
    monkeypatch.setattr(secret_box, "DEFAULT_KEY_PATH", str(key_path))
    return {"config_path": config_path, "key_path": key_path}


def _absent(text: str, s: dict[str, str]) -> bool:
    return not any(v in text for v in s.values())


# ── Encrypted configuration fields ──────────────────────────────────────────


def test_rtsp_and_roboflow_fields_are_encrypted_at_rest(store, secrets) -> None:
    import config_store
    import secret_box

    secret_box.write_key_if_missing(str(store["key_path"]))
    config_store.save(_plaintext_cfg(secrets))

    on_disk = store["config_path"].read_text()
    assert _absent(on_disk, secrets)
    raw = yaml.safe_load(on_disk)
    assert secret_box.is_encrypted(raw["cameras"][0]["rtsp_url"])
    assert secret_box.is_encrypted(raw["training"]["sources"]["roboflow"]["api_key"])
    # Readers decrypt: integrations still get the working value.
    loaded = config_store.load()
    assert loaded["cameras"][0]["rtsp_url"].endswith("@192.0.2.10:554/stream1")
    assert secrets["rtsp"] in loaded["cameras"][0]["rtsp_url"]
    assert loaded["training"]["sources"]["roboflow"]["api_key"] == secrets["roboflow"]


def test_startup_migrates_plaintext_when_key_exists(store, secrets) -> None:
    import config_store
    import main
    import secret_box

    secret_box.write_key_if_missing(str(store["key_path"]))
    store["config_path"].write_text(yaml.safe_dump(_plaintext_cfg(secrets)))

    main._migrate_plaintext_secrets()

    assert config_store.secret_migration_state == "migrated"
    assert _absent(store["config_path"].read_text(), secrets)
    assert config_store.load()["notifications"]["channels"][1]["token"] == secrets["ntfy"]


def test_migration_without_key_is_explicitly_pending(store, secrets) -> None:
    import config_store

    original = yaml.safe_dump(_plaintext_cfg(secrets))
    store["config_path"].write_text(original)

    assert config_store.migrate_plaintext_secrets() == "pending_key"
    assert config_store.secret_migration_state == "pending_key"
    assert store["config_path"].read_text() == original


def test_migration_with_foreign_ciphertext_leaves_file_untouched(store, secrets) -> None:
    import config_store
    import secret_box

    cfg = _plaintext_cfg(secrets)
    secret_box.encrypt_in_place(cfg, secret_box.generate_key())
    original = yaml.safe_dump(cfg)
    store["config_path"].write_text(original)
    secret_box.write_key_if_missing(str(store["key_path"]))

    assert config_store.migrate_plaintext_secrets() == "undecryptable"
    assert store["config_path"].read_text() == original


# ── Viewer responses ────────────────────────────────────────────────────────


def test_viewer_config_page_hides_credentials_but_shows_status(
    client, monkeypatch, secrets,
) -> None:
    cfg = _plaintext_cfg(secrets)
    monkeypatch.setattr("config_store.load", lambda: cfg)
    monkeypatch.setattr("config_store.load_cached", lambda **_kw: cfg)
    monkeypatch.setattr("routes.config.has_admin_access", lambda _req: False)

    resp = client.get("/config")

    assert resp.status_code == 200
    assert _absent(resp.text, secrets)
    assert "pond-north" in resp.text
    assert "***REDACTED***" in resp.text


class _FakePubSub:
    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)

    async def subscribe(self, _channel: str) -> None:
        return None

    async def unsubscribe(self, _channel: str) -> None:
        return None

    async def get_message(self, **_kw: Any) -> dict | None:
        if self._messages:
            return {"type": "message", "data": self._messages.pop(0)}
        _FakeRedis.drained = True
        return None


class _FakeRedis:
    buffered: list[str] = []
    live: list[str] = []
    drained = False

    def __init__(self, **_kw: Any) -> None:
        pass

    async def lrange(self, _key: str, _start: int, _end: int) -> list[str]:
        return list(self.buffered)

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self.live)

    async def aclose(self) -> None:
        return None


def test_log_viewer_masks_replayed_and_live_lines(client, monkeypatch, secrets) -> None:
    """Buffers written before the fix still replay masked; live lines too."""
    import contextlib

    import routes.admin as admin
    from starlette.requests import Request

    _FakeRedis.buffered = [
        f"Failed to open RTSP stream: rtsp://viewer:{secrets['rtsp']}@192.0.2.10/s1",
        f'{{"auth_token": "{secrets["hook"]}"}}',
    ]
    _FakeRedis.live = [f"POST https://ntfy.example.com/pond?token={secrets['ntfy']}"]
    _FakeRedis.drained = False

    @contextlib.asynccontextmanager
    async def _no_limit(*_a: Any, **_kw: Any):
        yield

    async def _disconnected(_self: Request) -> bool:
        return _FakeRedis.drained

    monkeypatch.setattr(admin.aioredis, "Redis", _FakeRedis)
    monkeypatch.setattr(admin, "sse_connection", _no_limit)
    monkeypatch.setattr(Request, "is_disconnected", _disconnected)

    resp = client.get("/admin/logs/stream?service=detector")

    assert resp.status_code == 200
    assert _absent(resp.text, secrets)
    # Legitimate status survives: host, path and the event text stay readable.
    assert "rtsp://viewer:***@192.0.2.10/s1" in resp.text
    assert "https://ntfy.example.com/pond?token=***" in resp.text


# ── Source logs ─────────────────────────────────────────────────────────────


def test_source_log_filter_masks_before_any_handler_output(secrets) -> None:
    import log_redact

    stream = io.StringIO()
    logger = logging.getLogger(f"fdy0570.{uuid.uuid4().hex}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler(stream))
    log_redact.install(logger)

    logger.warning("Failed to open RTSP stream: %s", f"rtsp://viewer:{secrets['rtsp']}@cam/s1")
    logger.info("cmd --roboflow-key %s", secrets["roboflow"])
    logger.info("https://discord.com/api/webhooks/42/%s", secrets["hook"])
    try:
        raise RuntimeError(f"Authorization: Bearer {secrets['ntfy']}")
    except RuntimeError:
        logger.exception("send failed")

    out = stream.getvalue()
    assert _absent(out, secrets)
    assert "rtsp://viewer:***@cam/s1" in out
    assert "send failed" in out


def test_unsafe_url_error_does_not_echo_the_url(secrets, monkeypatch) -> None:
    import url_safety

    monkeypatch.setattr(url_safety, "_resolve", lambda _host: ["10.0.0.5"])
    with pytest.raises(url_safety.UnsafeURLError) as info:
        url_safety.validate_external_url(
            f"https://internal.example/hook/{secrets['hook']}", allow_internal=False,
        )
    assert secrets["hook"] not in str(info.value)
    assert "internal.example" in str(info.value)


# ── Setup-created credentials ───────────────────────────────────────────────


def _find_upwards(relative: str) -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / relative
        if candidate.is_file():
            return candidate
    return None


def test_setup_env_is_owner_only_on_create_and_rerun(tmp_path: Path) -> None:
    setup_sh = _find_upwards("setup.sh")
    if setup_sh is None:
        pytest.skip("setup.sh is not shipped in this image")
    text = setup_sh.read_text()
    assert "\numask 077\n" in text

    # Execute the real .env permission statements in order against a
    # pre-existing world-readable .env (the rerun case).
    env_file = tmp_path / ".env"
    env_file.write_text("HTTP_PORT=80\n")
    os.chmod(env_file, 0o644)
    lines = [ln.strip() for ln in text.splitlines()]
    statements = [ln for ln in lines if ln in ("umask 077", "chmod 0600 .env")]
    assert statements.count("chmod 0600 .env") >= 2  # create path and rerun path
    subprocess.run(["bash", "-c", "; ".join(statements)], cwd=tmp_path, check=True)
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    created = tmp_path / "fresh"
    created.mkdir()
    subprocess.run(
        ["bash", "-c", "umask 077; printf 'K=V\\n' > .env"], cwd=created, check=True,
    )
    assert stat.S_IMODE((created / ".env").stat().st_mode) == 0o600
