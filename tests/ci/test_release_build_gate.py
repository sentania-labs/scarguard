"""Exercise the release gate's workflow-to-helper authentication boundary."""
from __future__ import annotations

import io
import json
import runpy
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(name: str, status: str, conclusion: str | None) -> dict[str, object]:
    return {"name": name, "status": status, "conclusion": conclusion}


GATE = _run("Build workflow gate", "in_progress", None)
OK_WEB = _run("Build web image", "completed", "success")
OK_DETECTOR = _run("Build detector image (x86)", "completed", "success")
BAD_NOTIFIER = _run("Build notifier image", "completed", "failure")
PENDING_NOTIFIER = _run("Build notifier image", "in_progress", None)
SMOKE = _run("Compose smoke test", "completed", "success")


@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        ([[OK_WEB]], 0),
        ([[_run("Build web image", "completed", "failure")]], 1),
        ([[_run("Build web image", "in_progress", None)]], 1),
        ([[GATE, SMOKE, OK_DETECTOR, OK_WEB]], 0),
        ([[GATE, OK_DETECTOR, BAD_NOTIFIER, OK_WEB]], 1),
        ([[GATE, OK_DETECTOR, PENDING_NOTIFIER, OK_WEB]], 1),
        ([[GATE, OK_DETECTOR], [OK_WEB]], 0),
        ([[GATE, OK_DETECTOR], [BAD_NOTIFIER]], 1),
        ([[GATE, SMOKE]], 1),
    ],
)
def test_workflow_authenticates_helper_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pages: list[list[dict[str, object]]],
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
    total = sum(len(page) for page in pages)

    def urlopen(request: urllib.request.Request) -> io.BytesIO:
        requests.append(request)
        if request.get_header("Authorization") != "Bearer test-token":
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)
        if gate["permissions"].get("checks") != "read":
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)
        query = parse_qs(urlparse(request.full_url).query)
        index = int(query["page"][0]) - 1
        runs = pages[index] if index < len(pages) else []
        return io.BytesIO(json.dumps({"total_count": total, "check_runs": runs}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(REPO_ROOT / ".github/scripts/check-build-status.py"), run_name="__main__")
    output = capsys.readouterr().out
    assert "401" not in output and "403" not in output, output
    assert exit_info.value.code == expected, output
    assert len(requests) == len(pages)
    for request in requests:
        assert request.full_url.startswith("https://api.github.com/repos/example/pond/commits/test-commit/check-runs?")
