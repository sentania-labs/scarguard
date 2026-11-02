"""Collecting several services' tests in one pytest process must work (issue #200).

Every service ships a top-level ``src/main.py``; before the per-service
isolation in each ``tests/conftest.py``, the first service collected owned
``sys.modules["main"]`` and the next one aborted collection.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICES = ("training-controller", "trainer")


def test_two_services_collect_together_in_one_process() -> None:
    test_dirs = [REPO_ROOT / "services" / name / "tests" for name in SERVICES]
    if not all(d.is_dir() for d in test_dirs):
        pytest.skip("needs the repository checkout with both services' tests")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
        + [str(d) for d in test_dirs],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    match = re.search(r"(\d+) tests? collected", result.stdout)
    assert match is not None, result.stdout
    assert int(match.group(1)) > 0
