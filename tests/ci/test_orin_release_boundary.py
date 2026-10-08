"""Regression tests for the production Orin workflow boundary."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
ORIN_LABELS = {"self-hosted", "linux", "arm64", "jetson"}


class WorkflowLoader(yaml.SafeLoader):
    """YAML loader that preserves GitHub Actions' ``on`` key as a string."""


WorkflowLoader.yaml_implicit_resolvers = copy.deepcopy(
    yaml.SafeLoader.yaml_implicit_resolvers
)
for resolver_key, resolvers in WorkflowLoader.yaml_implicit_resolvers.items():
    WorkflowLoader.yaml_implicit_resolvers[resolver_key] = [
        resolver
        for resolver in resolvers
        if resolver[0] != "tag:yaml.org,2002:bool"
    ]


def load_workflow(name: str) -> dict[str, Any]:
    """Parse a repository workflow with GitHub-compatible key handling."""
    workflow = yaml.load(
        (WORKFLOW_DIR / name).read_text(encoding="utf-8"), Loader=WorkflowLoader
    )
    assert isinstance(workflow, dict)
    return workflow


def github_pattern_matches(pattern: str, value: str) -> bool:
    """Match the GitHub filter constructs used by these workflow tag filters."""
    regex = ""
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "[":
            end = pattern.index("]", index)
            regex += pattern[index : end + 1]
            index = end + 1
            continue
        if character == "*":
            regex += ".*"
        elif character == "?":
            regex += "."
        elif character == "+":
            regex += "+"
        else:
            regex += re.escape(character)
        index += 1
    return re.fullmatch(regex, value) is not None


def workflow_is_triggered(
    workflow: dict[str, Any], event: str, *, ref_type: str = "", ref_name: str = ""
) -> bool:
    """Evaluate the event and ref filters represented in a parsed workflow."""
    triggers = workflow.get("on", {})
    if isinstance(triggers, str):
        return triggers == event
    if event not in triggers:
        return False
    configuration = triggers[event]
    if event != "push" or not isinstance(configuration, dict):
        return True

    filter_name = "tags" if ref_type == "tag" else "branches"
    patterns = configuration.get(filter_name)
    if patterns is None:
        return ref_type != "tag" or "branches" not in configuration
    return any(github_pattern_matches(pattern, ref_name) for pattern in patterns)


def job_guard_allows(job: dict[str, Any], event: dict[str, str]) -> bool:
    """Evaluate the conjunction used to protect the release Orin job."""
    expression = job.get("if")
    if expression is None:
        return True
    clauses = [clause.strip() for clause in expression.split("&&")]
    results: list[bool] = []
    for clause in clauses:
        equality = re.fullmatch(r"github\.(\w+) == '([^']+)'", clause)
        starts_with = re.fullmatch(
            r"startsWith\(github\.(\w+), '([^']+)'\)", clause
        )
        if equality:
            results.append(event.get(equality.group(1)) == equality.group(2))
        elif starts_with:
            results.append(
                event.get(starts_with.group(1), "").startswith(starts_with.group(2))
            )
        else:
            raise AssertionError(f"Unsupported Orin job guard clause: {clause}")
    return all(results)


def orin_jobs_for_event(
    event_name: str, *, ref_type: str = "", ref_name: str = ""
) -> list[tuple[str, str]]:
    """Return parsed workflow/job pairs eligible to schedule the production Orin."""
    eligible: list[tuple[str, str]] = []
    event = {
        "event_name": event_name,
        "ref_type": ref_type,
        "ref_name": ref_name,
    }
    for workflow_path in sorted(WORKFLOW_DIR.glob("*.yml")):
        workflow = load_workflow(workflow_path.name)
        if not workflow_is_triggered(
            workflow, event_name, ref_type=ref_type, ref_name=ref_name
        ):
            continue
        for job_name, job in workflow.get("jobs", {}).items():
            runner = job.get("runs-on")
            labels = set(runner) if isinstance(runner, list) else {runner}
            if ORIN_LABELS.issubset(labels) and job_guard_allows(job, event):
                eligible.append((workflow_path.name, job_name))
    return eligible


@pytest.mark.parametrize(
    ("event_name", "ref_type", "ref_name"),
    [
        ("pull_request", "branch", "main"),
        ("push", "branch", "main"),
        ("workflow_dispatch", "branch", "main"),
        ("schedule", "branch", "main"),
    ],
)
def test_non_release_events_cannot_schedule_orin(
    event_name: str, ref_type: str, ref_name: str
) -> None:
    assert orin_jobs_for_event(
        event_name, ref_type=ref_type, ref_name=ref_name
    ) == []


def test_only_version_tag_push_can_schedule_release_orin() -> None:
    assert orin_jobs_for_event("push", ref_type="tag", ref_name="v1.2.3") == [
        ("release.yml", "release-detector")
    ]
    assert orin_jobs_for_event("push", ref_type="tag", ref_name="candidate") == []
    assert orin_jobs_for_event(
        "workflow_dispatch", ref_type="tag", ref_name="v1.2.3"
    ) == []


def test_build_keeps_hosted_validation_without_orin_dependency() -> None:
    build = load_workflow("build.yml")
    jobs = build["jobs"]

    assert jobs["build-detector-x86"]["runs-on"] == "ubuntu-latest"
    assert jobs["build-trainer"]["runs-on"] == "ubuntu-24.04-arm"
    compose = jobs["compose-smoke-test"]
    assert compose["runs-on"] == "ubuntu-latest"
    assert set(compose["needs"]) == {
        "build-detector-x86",
        "build-web",
        "build-notifier",
        "build-deterrent",
        "build-backup",
        "build-caddy",
        "build-log-streamer",
        "build-training-controller",
    }
    assert workflow_is_triggered(build, "pull_request", ref_type="branch", ref_name="main")
    assert workflow_is_triggered(build, "push", ref_type="branch", ref_name="main")
    assert workflow_is_triggered(
        build, "workflow_dispatch", ref_type="branch", ref_name="main"
    )


def test_cleanup_workflow_is_inert_and_read_only() -> None:
    cleanup = load_workflow("cleanup.yml")
    assert cleanup["on"] == {}
    assert cleanup["jobs"] == {}
    assert cleanup["permissions"] == {"contents": "read"}
