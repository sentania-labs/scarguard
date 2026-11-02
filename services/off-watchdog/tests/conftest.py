import sys
from importlib import util
from pathlib import Path
from types import ModuleType

import pytest

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
