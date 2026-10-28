"""Exercise the release gate's workflow-to-helper authentication boundary."""
from __future__ import annotations

import io
import json
import runpy
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("status", "conclusion", "expected"),
    [
        ("completed", "success", 0),
        ("completed", "failure", 1),
        ("in_progress", None, 1),
    ],
)
def test_workflow_authenticates_helper_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    conclusion: str | None,
    expected: int,
) -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/release.yml").read_text())
    gate = workflow["jobs"]["build-gate"]
    step = next(step for step in gate["steps"] if "run" in step)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/pond")
    monkeypatch.setenv("GITHUB_SHA", "test-commit")
    for name, value in step.get("env", {}).items():
        monkeypatch.setenv(name, value.replace("${{ secrets.GITHUB_TOKEN }}", "test-token"))

    requests = []

    def urlopen(request: urllib.request.Request) -> io.BytesIO:
        requests.append(request)
        if request.get_header("Authorization") != "Bearer test-token":
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)
        if gate["permissions"].get("checks") != "read":
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)
        return io.BytesIO(json.dumps({"check_runs": [
            {"name": "Build web image", "status": status, "conclusion": conclusion},
        ]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(REPO_ROOT / ".github/scripts/check-build-status.py"), run_name="__main__")
    output = capsys.readouterr().out
    assert "401" not in output and "403" not in output, output
    assert exit_info.value.code == expected, output
    assert len(requests) == 1
    assert requests[0].full_url == "https://api.github.com/repos/example/pond/commits/test-commit/check-runs"
