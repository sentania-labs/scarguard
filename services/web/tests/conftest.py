"""
Set critical env vars before any app module is imported.
db.py and config_store.py read CONFIG_PATH / DB_PATH at module level,
so these must be set before the first import of those modules.
"""

import os
import sys
from importlib import util
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


def _prioritize(dirs: list[str]) -> None:
    """Move this service's own trees to the front of ``sys.path``."""
    for _dir in reversed(dirs):
        if _dir in sys.path:
            sys.path.remove(_dir)
        sys.path.insert(0, _dir)


def _load(stem: str) -> ModuleType:
    """Load ``src/<stem>.py`` for this service once and keep that object."""
    path = SRC_DIR / f"{stem}.py"
    existing = _CACHE.get(stem) or sys.modules.get(stem)
    if existing is not None and str(getattr(existing, "__file__", "")) == str(path):
        return existing
    saved = list(sys.path)
    _prioritize(_OWN_DIRS)
    try:
        sys.modules.pop(stem, None)
        spec = util.spec_from_file_location(stem, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path}")
        module = util.module_from_spec(spec)
        sys.modules[stem] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(stem, None)
            raise
        _CACHE[stem] = module
        return module
    finally:
        sys.path[:] = saved


def _bind() -> None:
    """Point this package's plain module names at this service's own modules.

    Every service ships a top-level ``src/main.py``, so in one process all of
    them compete for the plain name ``main`` (issue #200) and only the first
    module collected wins.  Each service loads its own modules once and then
    re-points the shared names at those same objects, so test modules and
    ``patch("main.x")`` keep seeing the object they were import-bound to.
    """
    for stem in _STEMS:
        sys.modules[stem] = _load(stem)


def pytest_collectstart(collector: pytest.Collector) -> None:
    """Rebind this package's plain module names before its test modules import."""
    _path = getattr(collector, "path", None)
    if _path is not None:
        _this = Path(str(_path))
        if _this == TESTS_DIR or TESTS_DIR in _this.parents:
            _bind()


@pytest.fixture(autouse=True)
def _service_modules() -> None:
    """Keep the shared plain names pointed at this service's own modules."""
    _bind()


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
