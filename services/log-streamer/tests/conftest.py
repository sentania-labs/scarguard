"""Shared fixtures for log-streamer service tests."""

import sys
from pathlib import Path

# The shared test-support helper lives in <repo>/shared (repo checkout) or
# /app/shared (service image), found without any hand-set PYTHONPATH.
_SHARED = next(
    p / "shared"
    for p in Path(__file__).resolve().parents
    if (p / "shared" / "service_test_isolation.py").is_file()
)
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from service_test_isolation import isolate  # noqa: E402

pytest_make_collect_report, _service_modules = isolate(__file__)
