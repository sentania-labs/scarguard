"""Test-only: keep each service's plain module names apart in one pytest process.

Every service ships a top-level ``src/main.py`` (and some share other stems, such
as ``actuation_db``), so running several services' tests in one process makes
them compete for the same ``sys.modules`` entries (issue #200).  A service's
``tests/conftest.py`` calls :func:`isolate` to get the two pytest hooks that give
each service its own modules.  Nothing is imported eagerly: only modules the
service's tests import are ever loaded.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Generator
from pathlib import Path
from types import ModuleType

import pytest

SHARED_DIR = Path(__file__).resolve().parent


def isolate(
    conftest_file: str,
) -> tuple[
    Callable[[pytest.Collector], Generator[None, None, None]],
    Callable[[], Generator[None, None, None]],
]:
    """Return ``(pytest_make_collect_report, autouse fixture)`` for one service.

    Assign both at module level in the service's ``conftest.py``.
    """
    tests_dir = Path(conftest_file).resolve().parent
    src_dir = tests_dir.parent / "src"
    own_dirs = [str(src_dir), str(SHARED_DIR)]
    stems = sorted(p.stem for p in src_dir.glob("*.py"))
    cache: dict[str, ModuleType] = {}

    def prioritize() -> None:
        for _dir in reversed(own_dirs):
            if _dir in sys.path:
                sys.path.remove(_dir)
            sys.path.insert(0, _dir)

    def own(module: ModuleType) -> bool:
        file = getattr(module, "__file__", None)
        return file is not None and Path(file).resolve().parent == src_dir

    def harvest() -> None:
        for stem in stems:
            module = sys.modules.get(stem)
            if module is not None and own(module):
                cache[stem] = module

    def bind() -> None:
        # Restore modules this service already imported (so test modules and
        # ``patch("main.x")`` keep seeing the object they were bound to) and
        # drop another service's module under the same name so the next import
        # loads this service's own.
        prioritize()
        for stem in stems:
            if stem in cache:
                sys.modules[stem] = cache[stem]
            elif stem in sys.modules and not own(sys.modules[stem]):
                del sys.modules[stem]

    prioritize()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_make_collect_report(collector: pytest.Collector) -> Generator[None, None, None]:
        path = getattr(collector, "path", None)
        inside = path is not None and (
            Path(str(path)) == tests_dir or tests_dir in Path(str(path)).parents
        )
        if inside:
            bind()
        yield
        if inside:
            harvest()

    @pytest.fixture(autouse=True, name="_service_modules")
    def service_modules() -> Generator[None, None, None]:
        bind()
        yield
        harvest()

    return pytest_make_collect_report, service_modules
