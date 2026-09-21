"""
Set critical env vars before any app module is imported.
db.py and config_store.py read CONFIG_PATH / DB_PATH at module level,
so these must be set before the first import of those modules.
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# ── Point app at temp paths so tests never touch /data or /config ──────────
os.environ["CONFIG_PATH"] = "/tmp/sg-test.yml"
os.environ["DB_PATH"] = "/tmp/sg-test.db"
os.environ["SNAPSHOT_DIR"] = "/tmp/sg-test-snapshots"
os.environ["MODELS_DIR"] = "/tmp/sg-test-models"
os.environ["AUTH_DB_PATH"] = "/tmp/sg-test-auth.db"

Path("/tmp/sg-test-snapshots").mkdir(exist_ok=True)
Path("/tmp/sg-test-models").mkdir(exist_ok=True)

import pytest  # noqa: E402

MOCK_CONFIG = {
    "system": {"armed": True, "log_level": "info", "auth": {"enabled": False}},
    "cameras": [
        {
            "name": "pond-north",
            "rtsp_url": "rtsp://localhost/test",
            "enabled": True,
            "resolution": 720,
        }
    ],
    "detection": {
        "model_path": "/models/best.pt",
        "confidence_threshold": 0.25,
        "target_classes": ["great_blue_heron"],
        "cooldown_seconds": 30,
        "frame_skip": 2,
    },
    "redis": {"host": "localhost", "port": 6379},
    "notifications": {},
}


@pytest.fixture()
def client(monkeypatch):
    """FastAPI TestClient with all external I/O mocked out."""
    monkeypatch.setattr("config_store.load", lambda: MOCK_CONFIG)
    monkeypatch.setattr("config_store.load_cached", lambda **_kw: MOCK_CONFIG)
    monkeypatch.setattr("config_store.save", lambda _cfg: None)
    monkeypatch.setattr("config_store.set_armed", lambda _armed: None)
    monkeypatch.setattr("db.get_latest_event", lambda: None)
    monkeypatch.setattr("db.count_events", lambda **_kw: 0)
    monkeypatch.setattr("db.get_events", lambda **_kw: [])
    monkeypatch.setattr("db.get_latest_snapshots_by_camera", lambda: {})

    from fastapi.testclient import TestClient
    from main import app

    c = TestClient(app)
    # Prime CSRF cookie via a GET request, then set the header on the client
    # so all subsequent requests include the token automatically.
    c.get("/")
    csrf_token = c.cookies.get("csrf_token", "")
    c.headers["X-CSRF-Token"] = csrf_token
    return c


@pytest.fixture(autouse=True)
def isolated_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Exercise the real limiter without consuming any external Redis counters."""
    from rate_limit import RateLimiter

    counts: dict[str, int] = {}
    expiries: dict[str, int] = {}
    redis = MagicMock()

    def incr(key: str) -> int:
        counts[key] = counts.get(key, 0) + 1
        return counts[key]

    redis.incr.side_effect = incr
    redis.expire.side_effect = lambda key, seconds: expiries.setdefault(key, seconds)
    redis.ttl.side_effect = lambda key: expiries.get(key, -1)
    monkeypatch.setattr("rate_limit_dep._limiter", RateLimiter(redis))
    return redis


@pytest.fixture(autouse=True)
def isolated_dashboard_redis(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """CSRF priming/dashboard routes must not read or modify an external Redis."""
    values: dict[str, str] = {}
    redis = AsyncMock()
    redis.get.side_effect = lambda key: values.get(key)
    redis.set.side_effect = lambda key, value: values.update({key: value})
    redis.delete.side_effect = lambda key: values.pop(key, None)
    monkeypatch.setattr("routes.dashboard._redis_client", lambda _cfg: redis)
    return redis
