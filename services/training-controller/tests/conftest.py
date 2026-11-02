import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

import pytest

# Repo checkout: services/<name>/tests -> repo root holds shared/.
# Service image: /app/tests -> /app holds shared/ and src/.
TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"
_SHARED = next(
    (p / "shared" for p in SRC_DIR.parents if (p / "shared" / "training_safety.py").is_file()),
    None,
)
for _p in (str(SRC_DIR), str(_SHARED)) if _SHARED else (str(SRC_DIR),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

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
