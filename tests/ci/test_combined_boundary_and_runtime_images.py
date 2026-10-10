"""Tests for the combined CI boundary and runtime image requirements."""

import re
from pathlib import Path

import pytest

from tests.ci.test_orin_release_boundary import orin_jobs_for_event

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("event_name", "ref_type", "ref_name"),
    [
        ("pull_request", "branch", "main"),
        ("push", "branch", "main"),
        ("workflow_dispatch", "branch", "main"),
    ],
)
def test_no_orin_jobs_on_standard_events(event_name: str, ref_type: str, ref_name: str) -> None:
    jobs = orin_jobs_for_event(event_name, ref_type=ref_type, ref_name=ref_name)
    assert not jobs, f"Found Orin jobs scheduled for {event_name}: {jobs}"


def test_no_runtime_stage_pip_install_without_uninstall() -> None:
    services_dir = REPO_ROOT / "services"
    for dockerfile in services_dir.rglob("Dockerfile*"):
        content = dockerfile.read_text(encoding="utf-8")
        run_statements = re.findall(r"RUN\s+((?:[^\n]*\\\n)*[^\n]*)", content)
        for stmt in run_statements:
            if "pip install --upgrade pip" in stmt:
                assert "pip uninstall" in stmt, (
                    f"Missing pip uninstall in {dockerfile.relative_to(REPO_ROOT)}"
                )
