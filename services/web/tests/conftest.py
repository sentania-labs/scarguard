"""
Set critical env vars before any app module is imported.
db.py and config_store.py read CONFIG_PATH / DB_PATH at module level,
so these must be set before the first import of those modules.
"""

import os
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

# Repo checkout: services/<name>/tests -> repo root holds shared/.
# Service image: /app/tests -> /app holds shared/ and src/.
TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"
_SHARED = next(
    (p / "shared" for p in SRC_DIR.parents if (p / "shared" / "models.py").is_file()), None
)
for _p in (str(SRC_DIR), str(_SHARED)) if _SHARED else (str(SRC_DIR),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Point app at temp paths so tests never touch /data or /config ──────────
os.environ["CONFIG_PATH"] = "/tmp/sg-test.yml"
os.environ["DB_PATH"] = "/tmp/sg-test.db"
os.environ["SNAPSHOT_DIR"] = "/tmp/sg-test-snapshots"
os.environ["MODELS_DIR"] = "/tmp/sg-test-models"
os.environ["MODEL_STORE_DIR"] = "/tmp/sg-test-model-store"
os.environ["AUTH_DB_PATH"] = "/tmp/sg-test-auth.db"

Path("/tmp/sg-test-snapshots").mkdir(exist_ok=True)
Path("/tmp/sg-test-models").mkdir(exist_ok=True)

import pytest  # noqa: E402

# This service's own trees, in the order plain imports should resolve them.
_OWN_DIRS = [str(SRC_DIR)] + ([str(_SHARED)] if _SHARED else [])
_STEMS = sorted(p.stem for p in SRC_DIR.glob("*.py"))
# One module object per plain name, keyed by that name (issue #200).
_CACHE: dict[str, ModuleType] = {}


def _prioritize() -> None:
    """Move this service's own trees to the front of ``sys.path``."""
    for _dir in reversed(_OWN_DIRS):
        if _dir in sys.path:
            sys.path.remove(_dir)
        sys.path.insert(0, _dir)


def _own(module: ModuleType) -> bool:
    _file = getattr(module, "__file__", None)
    return _file is not None and Path(_file).resolve().parent == SRC_DIR


def _harvest() -> None:
    """Remember the modules of this service that have been imported so far."""
    for stem in _STEMS:
        module = sys.modules.get(stem)
        if module is not None and _own(module):
            _CACHE[stem] = module


def _bind() -> None:
    """Point this package's plain module names at this service's own modules.

    Every service ships a top-level ``src/main.py``, so in one process all of
    them compete for the plain name ``main`` (issue #200) and only the first
    module collected wins.  Nothing is imported here: modules already imported
    for this service are restored (so test modules and ``patch("main.x")`` keep
    seeing the object they were import-bound to), and another service's module
    under the same name is dropped so the next import loads this service's own.
    """
    _prioritize()
    for stem in _STEMS:
        if stem in _CACHE:
            sys.modules[stem] = _CACHE[stem]
        elif stem in sys.modules and not _own(sys.modules[stem]):
            del sys.modules[stem]


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector: pytest.Collector) -> Generator[None, None, None]:
    """Bind this service's names while its test modules are imported."""
    _path = getattr(collector, "path", None)
    _inside = _path is not None and (
        Path(str(_path)) == TESTS_DIR or TESTS_DIR in Path(str(_path)).parents
    )
    if _inside:
        _bind()
    yield
    if _inside:
        _harvest()


@pytest.fixture(autouse=True)
def _service_modules() -> Generator[None, None, None]:
    """Keep the shared plain names pointed at this service's own modules."""
    _bind()
    yield
    _harvest()


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

    def eval_script(script: str, numkeys: int, key: str, window: int) -> list[int]:
        # Test double for the limiter's single atomic INCR+EXPIRE script.
        count = incr(key)
        expiries.setdefault(key, int(window))
        return [count, expiries[key]]

    redis.eval.side_effect = eval_script
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
