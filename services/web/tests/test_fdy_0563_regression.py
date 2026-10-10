"""FDY-0563 regression: per-service Redis ACL identities, non-evicting safety state.

Findings SG-02 (web/log containers can forge detector events), SG-03 (one shared
all-powerful Redis credential) and SG-26 (pause/rearm/quota/lease state in an
evictable cache, non-atomic counter+TTL).

Three layers, each exercising real handlers and artifacts:

1. Code paths importable in the service image (``/app/tests``): the shared
   client factory, the rate limiter, the notify-request signer and the live
   FastAPI "send test notification" route.
2. Repository artifacts, located relative to this file and skipped cleanly
   when absent: ``docker-compose.yml``, ``config/redis-acl.conf``,
   ``config/redis-entrypoint.sh`` (run with a stub ``redis-server``) and
   ``scripts/migrate-redis-acl.sh``.
3. A disposable real Redis booted through the real entrypoint, proving
   allowed and denied operations per service identity plus pub/sub and
   reconnect flows. Needs a Redis 7 ``redis-server`` on ``PATH`` or in
   ``SCARGUARD_REDIS_SERVER``; skips otherwise.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import redis as redis_lib
import yaml

SERVICES = [
    "detector", "web", "notifier", "deterrent", "off-watchdog", "backup",
    "log-streamer", "training-controller", "trainer",
]
HMAC_HOLDERS = {"detector", "web", "notifier", "deterrent", "backup"}


def _var(service: str) -> str:
    return "REDIS_PASSWORD_" + service.upper().replace("-", "_")


def find_repo_root() -> Path | None:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "config/redis-acl.conf").is_file() and (candidate / "docker-compose.yml").is_file():
            return candidate
    return None


REPO = find_repo_root()
needs_repo = pytest.mark.skipif(REPO is None, reason="repository artifacts not present (service image runs tests at /app/tests)")


def _secret() -> str:
    return secrets.token_urlsafe(24)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ── 1. Code paths available everywhere ─────────────────────────────────────

def test_shared_client_authenticates_as_named_service_user(monkeypatch: pytest.MonkeyPatch) -> None:
    from redis_client import _resolve_params, make_sync_client, redis_auth

    token = _secret()
    monkeypatch.setenv("REDIS_USERNAME", "web")
    monkeypatch.setenv("REDIS_PASSWORD", token)
    assert redis_auth() == {"username": "web", "password": token}
    kwargs = make_sync_client({"host": "h", "port": 1}).connection_pool.connection_kwargs
    assert (kwargs["username"], kwargs["password"], kwargs["host"]) == ("web", token, "h")

    # Fail closed: a named user with no credential still authenticates as that
    # user (and is rejected by Redis) instead of silently becoming admin.
    monkeypatch.setenv("REDIS_PASSWORD", "")
    assert _resolve_params()["username"] == "web" and "password" not in _resolve_params()


def test_rate_limiter_is_atomic_and_fails_closed() -> None:
    from rate_limit import INCR_WITH_TTL_SCRIPT, RateLimiter

    client = MagicMock()
    client.eval.return_value = [1, 60]
    assert RateLimiter(client).check("user:1", "arm", 3, 60) == (True, 0)
    client.eval.assert_called_once_with(INCR_WITH_TTL_SCRIPT, 1, "rl:v1:arm:user:1", 60)
    client.incr.assert_not_called()
    client.expire.assert_not_called()
    for error in (redis_lib.ResponseError("NOPERM"), redis_lib.ResponseError("OOM"), redis_lib.ConnectionError()):
        client.eval.side_effect = error
        assert RateLimiter(client).check("user:1", "arm", 3, 60) == (False, 60)


def test_notify_request_is_channel_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    from event_signing import derive_channel_key, verify_event
    from notify_request import NOTIFY_REQUEST_CHANNEL, sign_notify_request

    raw_key = secrets.token_bytes(32)
    monkeypatch.setenv("DETECTION_HMAC_KEY", base64.b64encode(raw_key).decode())
    envelope = json.loads(sign_notify_request({"class_name": "test_notification"}))
    assert verify_event(envelope, derive_channel_key(raw_key, NOTIFY_REQUEST_CHANNEL), channel=NOTIFY_REQUEST_CHANNEL)
    # A notify request replayed as a detection fails verification.
    assert not verify_event(envelope, derive_channel_key(raw_key, "scarguard:detections"), channel="scarguard:detections")


class _FakeAsyncRedis:
    published: list[tuple[str, str]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def publish(self, channel: str, data: str) -> int:
        _FakeAsyncRedis.published.append((channel, data))
        return 1

    async def close(self) -> None:
        return None


def test_web_test_notification_never_publishes_a_detection(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real /test-notification handler publishes a signed notify request only."""
    from routes import config as config_routes

    cfg = {
        "system": {"armed": True, "auth": {"enabled": False}},
        "notifications": {"channels": [{"name": "ops", "type": "webhook", "url": "http://x", "enabled": True}]},
        "redis": {"host": "localhost", "port": 6379},
    }
    monkeypatch.setattr("config_store.load_cached", lambda **_kw: cfg)
    raw_key = secrets.token_bytes(32)
    monkeypatch.setenv("DETECTION_HMAC_KEY", base64.b64encode(raw_key).decode())
    monkeypatch.setenv("REDIS_USERNAME", "web")
    monkeypatch.setattr(config_routes, "aioredis", SimpleNamespace(Redis=_FakeAsyncRedis))
    _FakeAsyncRedis.published.clear()

    path = next(r.path for r in client.app.routes if r.path.endswith("/test-notification"))
    res = client.post(path, json={"channel": "ops"})
    assert res.status_code == 200, res.text
    assert len(_FakeAsyncRedis.published) == 1
    channel, data = _FakeAsyncRedis.published[0]
    assert channel == "scarguard:notify:request"
    from event_signing import derive_channel_key, verify_event
    assert verify_event(json.loads(data), derive_channel_key(raw_key, channel), channel=channel)
    assert all(ch != "scarguard:detections" for ch, _ in _FakeAsyncRedis.published)


# ── 2. Repository artifacts ─────────────────────────────────────────────────

@needs_repo
def test_compose_delivers_one_dedicated_credential_per_service() -> None:
    assert REPO is not None
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())["services"]

    def env(name: str) -> dict[str, str]:
        raw = compose[name].get("environment", {})
        if isinstance(raw, list):
            return dict(item.split("=", 1) for item in raw)
        return {k: str(v) for k, v in raw.items()}

    for svc in SERVICES:
        e = env(svc)
        assert e.get("REDIS_USERNAME") == svc, svc
        assert e.get("REDIS_PASSWORD") == "${%s:-}" % _var(svc), svc
    redis_env = env("redis")
    assert redis_env["REDIS_PASSWORD"] == "${REDIS_PASSWORD:-}"
    for svc in SERVICES:
        assert redis_env[_var(svc)] == "${%s:-}" % _var(svc)
    assert "DETECTION_HMAC_KEY" not in redis_env
    for name in compose:
        if name != "redis":
            assert env(name).get("REDIS_PASSWORD") != "${REDIS_PASSWORD:-}", f"{name} holds the admin credential"
        assert ("DETECTION_HMAC_KEY" in env(name)) == (name in HMAC_HOLDERS), name
    assert compose["redis"]["command"] == ["sh", "/scarguard/redis-entrypoint.sh"]
    mounts = compose["redis"]["volumes"]
    assert "./config/redis-entrypoint.sh:/scarguard/redis-entrypoint.sh:ro" in mounts
    assert "./config/redis-acl.conf:/scarguard/redis-acl.conf:ro" in mounts


def _policy() -> dict[str, str]:
    assert REPO is not None
    rules: dict[str, str] = {}
    for line in (REPO / "config/redis-acl.conf").read_text().splitlines():
        if line and not line.startswith("#"):
            name, _, rest = line.partition(" ")
            rules[name] = rest
    return rules


@needs_repo
def test_policy_denies_forgery_and_safety_state_erasure() -> None:
    rules = _policy()
    assert set(rules) == set(SERVICES)
    for name, rest in rules.items():
        assert rest.startswith("resetchannels -@all"), name
        assert "+@all" not in rest and "+flushall" not in rest and "+config" not in rest, name
    web_root, _, web_selector = rules["web"].partition("(")
    assert "+publish" not in web_root and "&scarguard:detections" in web_root
    assert "+publish" in web_selector and "scarguard:detections" not in web_selector
    for name in ("web", "log-streamer", "notifier", "backup", "trainer"):
        assert "off-watchdog:lease" not in rules[name], name
    for name in ("web", "log-streamer", "notifier", "backup"):
        assert "~scarguard:detector:state" not in rules[name].replace("%R~scarguard:detector:state", ""), name
        assert "trainer:heartbeat" not in rules[name].replace("%R~scarguard:trainer:heartbeat", ""), name
    assert "+publish" not in rules["notifier"]
    det_root, _, det_selector = rules["deterrent"].partition("(")
    assert "+publish" not in det_root and "+publish" in det_selector
    assert "scarguard:detections" not in det_selector and "&scarguard:notify:request" in det_selector
    assert "&scarguard:detections" not in rules["log-streamer"]


def _run_entrypoint(tmp_path: Path, env_extra: dict[str, str], *args: str) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the real entrypoint with a stub redis-server that records its argv."""
    assert REPO is not None
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    argv_file = tmp_path / "argv.txt"
    (stub_dir / "redis-server").write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{argv_file}"\n')
    (stub_dir / "redis-server").chmod(0o755)
    acl_file = tmp_path / "scarguard.acl"
    env = {
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "SCARGUARD_REDIS_ACL_POLICY": str(REPO / "config/redis-acl.conf"),
        "SCARGUARD_REDIS_ACL_FILE": str(acl_file),
        **env_extra,
    }
    proc = subprocess.run(["sh", str(REPO / "config/redis-entrypoint.sh"), *args], env=env, capture_output=True, text=True, timeout=30)
    return proc, acl_file.read_text() if acl_file.exists() else ""


@needs_repo
def test_entrypoint_writes_digests_only_and_disables_noeviction(tmp_path: Path) -> None:
    admin = _secret()
    service_secrets = {svc: _secret() for svc in SERVICES}
    env = {"REDIS_PASSWORD": admin, **{_var(svc): val for svc, val in service_secrets.items()}}
    proc, acl = _run_entrypoint(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert f"user default on #{_sha256(admin)} ~* &* +@all" in acl
    for svc, val in service_secrets.items():
        assert f"user {svc} on #{_sha256(val)} resetchannels -@all" in acl, svc
        for text in (acl, proc.stdout, proc.stderr):
            assert val not in text and admin not in text
    argv = (tmp_path / "argv.txt").read_text().split("\n")
    assert argv[argv.index("--maxmemory-policy") + 1] == "noeviction"
    assert argv[argv.index("--aclfile") + 1] == str(tmp_path / "scarguard.acl")
    assert oct((tmp_path / "scarguard.acl").stat().st_mode & 0o777) == "0o600"


@needs_repo
def test_entrypoint_fails_closed_when_a_service_credential_is_missing(tmp_path: Path) -> None:
    admin = _secret()
    env = {"REDIS_PASSWORD": admin, **{_var(svc): _secret() for svc in SERVICES if svc != "log-streamer"}}
    proc, acl = _run_entrypoint(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert "user log-streamer off resetchannels -@all" in acl
    assert "REDIS_PASSWORD_LOG_STREAMER" in proc.stderr and "DISABLED" in proc.stderr
    assert admin not in proc.stderr
    assert "on nopass" not in acl and "user web on #" in acl


@needs_repo
def test_entrypoint_open_mode_keeps_acl_limits(tmp_path: Path) -> None:
    proc, acl = _run_entrypoint(tmp_path, {})
    assert proc.returncode == 0, proc.stderr
    assert "user default on nopass ~* &* +@all" in acl
    for svc in SERVICES:
        assert f"user {svc} on nopass resetchannels -@all" in acl
    assert "WITHOUT authentication" in proc.stderr


@needs_repo
def test_migration_script_backfills_without_printing_credentials(tmp_path: Path) -> None:
    assert REPO is not None
    env_file = tmp_path / ".env"
    admin = _secret()
    env_file.write_text(f"REDIS_PASSWORD={admin}\nREDIS_PASSWORD_WEB=\n")
    first = subprocess.run(["bash", str(REPO / "scripts/migrate-redis-acl.sh"), str(env_file)], capture_output=True, text=True, check=True)
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    assert values["REDIS_PASSWORD"] == admin
    for svc in SERVICES:
        assert len(values[_var(svc)]) == 32 and values[_var(svc)].isalnum(), svc
        assert values[_var(svc)] not in first.stdout + first.stderr
    assert len({values[_var(svc)] for svc in SERVICES}) == len(SERVICES)
    second = subprocess.run(["bash", str(REPO / "scripts/migrate-redis-acl.sh"), str(env_file)], capture_output=True, text=True, check=True)
    assert "already present" in second.stdout
    assert dict(line.split("=", 1) for line in env_file.read_text().splitlines()) == values


# ── 3. Disposable real Redis through the real entrypoint ───────────────────

REDIS_SERVER = os.environ.get("SCARGUARD_REDIS_SERVER") or shutil.which("redis-server")
needs_redis = pytest.mark.skipif(
    REPO is None or REDIS_SERVER is None,
    reason="no redis-server binary (set SCARGUARD_REDIS_SERVER) or repository artifacts absent",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _LiveRedis:
    def __init__(self, tmp_path: Path) -> None:
        assert REPO is not None and REDIS_SERVER is not None
        self.port = _free_port()
        self.admin = _secret()
        self.secrets = {svc: _secret() for svc in SERVICES}
        self.dir = tmp_path
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        os.symlink(REDIS_SERVER, bin_dir / "redis-server")
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "SCARGUARD_REDIS_ACL_POLICY": str(REPO / "config/redis-acl.conf"),
            "SCARGUARD_REDIS_ACL_FILE": str(tmp_path / "scarguard.acl"),
            "REDIS_PASSWORD": self.admin,
            **{_var(svc): val for svc, val in self.secrets.items()},
        }
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        assert REPO is not None
        self.proc = subprocess.Popen(
            ["sh", str(REPO / "config/redis-entrypoint.sh"), "--port", str(self.port), "--bind", "127.0.0.1",
             "--save", "", "--appendonly", "no", "--dir", str(self.dir)],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                self.admin_client().ping()
                return
            except redis_lib.RedisError:
                time.sleep(0.1)
        raise RuntimeError("disposable redis did not start")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait(timeout=10)
            self.proc = None

    def admin_client(self) -> redis_lib.Redis:
        return redis_lib.Redis(host="127.0.0.1", port=self.port, password=self.admin, decode_responses=True, socket_timeout=2)

    def as_service(self, monkeypatch: pytest.MonkeyPatch, svc: str) -> redis_lib.Redis:
        from redis_client import make_sync_client

        monkeypatch.setenv("REDIS_USERNAME", svc)
        monkeypatch.setenv("REDIS_PASSWORD", self.secrets[svc])
        return make_sync_client({"host": "127.0.0.1", "port": self.port}, socket_timeout=2)


@pytest.fixture()
def live_redis(tmp_path: Path):
    server = _LiveRedis(tmp_path)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _denied(fn, *args: Any) -> bool:
    try:
        fn(*args)
    except redis_lib.ResponseError as exc:
        return "NOPERM" in str(exc) or "WRONGPASS" in str(exc)
    except redis_lib.AuthenticationError:
        return True
    return False


@needs_redis
def test_live_identities_allowed_and_denied(live_redis: _LiveRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    from activation_lease import RedisActivationLeases, delete_if_unchanged, parse_signed_lease
    from rate_limit import RateLimiter

    admin = live_redis.admin_client()
    assert admin.config_get("maxmemory-policy")["maxmemory-policy"] == "noeviction"

    detector = live_redis.as_service(monkeypatch, "detector")
    assert detector.publish("scarguard:detections", "{}") >= 0
    assert detector.set("scarguard:detector:state", json.dumps({"state": "paused"}), ex=60)
    assert _denied(detector.delete, "scarguard:off-watchdog:lease:dev")

    web = live_redis.as_service(monkeypatch, "web")
    pubsub = web.pubsub()
    pubsub.subscribe("scarguard:detections")  # SSE event feed still works
    assert pubsub.get_message(timeout=1)["type"] == "subscribe"
    assert _denied(web.publish, "scarguard:detections", "{}"), "web forged a detection"
    assert web.publish("scarguard:snapshot:request", "{}") >= 0
    assert web.publish("scarguard:notify:request", "{}") >= 0
    assert web.get("scarguard:detector:state") is not None
    assert _denied(web.set, "scarguard:detector:state", "{}"), "web overwrote pause state"
    assert _denied(web.delete, "scarguard:trainer:heartbeat")
    assert _denied(web.delete, "scarguard:off-watchdog:lease:dev")
    assert _denied(web.flushall)
    limiter = RateLimiter(web)
    assert [limiter.check("user:1", "arm", 2, 60)[0] for _ in range(3)] == [True, True, False]
    assert 0 < web.ttl("rl:v1:arm:user:1") <= 60

    log_streamer = live_redis.as_service(monkeypatch, "log-streamer")
    assert log_streamer.lpush("scarguard:logs:buffer:web", "line") == 1
    assert log_streamer.publish("scarguard:logs:web", "line") >= 0
    assert _denied(log_streamer.publish, "scarguard:detections", "{}")
    assert _denied(log_streamer.delete, "scarguard:off-watchdog:lease:dev")
    assert _denied(log_streamer.set, "scarguard:rearm_at", "now")

    notifier = live_redis.as_service(monkeypatch, "notifier")
    assert _denied(notifier.publish, "scarguard:detections", "{}")
    assert _denied(notifier.get, "scarguard:detector:state")

    deterrent = live_redis.as_service(monkeypatch, "deterrent")
    assert _denied(deterrent.publish, "scarguard:detections", "{}"), "deterrent forged a detection"
    assert deterrent.publish("scarguard:actuations", "{}") >= 0
    assert deterrent.publish("scarguard:notify:request", "{}") >= 0
    key = secrets.token_bytes(32)
    leases = RedisActivationLeases(deterrent, key)
    lease = leases.arm("dev", 5.0)
    watchdog = live_redis.as_service(monkeypatch, "off-watchdog")
    found = list(watchdog.scan_iter(match="scarguard:off-watchdog:lease:*"))
    assert RedisActivationLeases.redis_key("dev") in found
    raw = watchdog.get(RedisActivationLeases.redis_key("dev"))
    assert parse_signed_lease(raw, key) is not None
    assert _denied(watchdog.publish, "scarguard:detections", "{}")
    assert delete_if_unchanged(watchdog, RedisActivationLeases.redis_key("dev"), raw)
    assert leases.clear(lease) is False  # already cleared by the watchdog

    assert _denied(live_redis.as_service(monkeypatch, "backup").get, "scarguard:detector:state")
    bad = redis_lib.Redis(host="127.0.0.1", port=live_redis.port, username="web", password=_secret(), socket_timeout=2)
    assert _denied(bad.ping)


@needs_redis
def test_live_pubsub_and_reconnect_flow(live_redis: _LiveRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    import redis_client

    monkeypatch.setattr(redis_client, "_MIN_RECONNECT_DELAY", 0.2)
    received: list[dict[str, Any]] = []
    stop = threading.Event()
    monkeypatch.setenv("REDIS_USERNAME", "notifier")
    monkeypatch.setenv("REDIS_PASSWORD", live_redis.secrets["notifier"])
    worker = threading.Thread(
        target=redis_client.reconnect_loop,
        args=({"host": "127.0.0.1", "port": live_redis.port}, ["scarguard:detections"], lambda _c, p: received.append(p), stop),
        daemon=True,
    )
    worker.start()
    detector = live_redis.as_service(monkeypatch, "detector")
    deadline = time.time() + 5
    while time.time() < deadline and not received:
        detector.publish("scarguard:detections", json.dumps({"n": 1}))
        time.sleep(0.2)
    assert received, "subscriber never received the detector's event"

    live_redis.stop()
    live_redis.start()
    received.clear()
    detector = live_redis.as_service(monkeypatch, "detector")
    deadline = time.time() + 10
    while time.time() < deadline and not received:
        detector.publish("scarguard:detections", json.dumps({"n": 2}))
        time.sleep(0.3)
    stop.set()
    assert received and received[-1]["n"] == 2, "subscriber did not reconnect after Redis restart"
