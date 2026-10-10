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


class WorkflowLoader(yaml.SafeLoader):
    """YAML loader that preserves GitHub Actions' ``on`` key as a string."""


WorkflowLoader.yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for resolver_key, resolvers in WorkflowLoader.yaml_implicit_resolvers.items():
    WorkflowLoader.yaml_implicit_resolvers[resolver_key] = [
        resolver for resolver in resolvers if resolver[0] != "tag:yaml.org,2002:bool"
    ]


def load_workflow(name: str) -> dict[str, Any]:
    """Parse a repository workflow with GitHub-compatible key handling."""
    workflow = yaml.load((WORKFLOW_DIR / name).read_text(encoding="utf-8"), Loader=WorkflowLoader)
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


PUSH_REF_FILTERS = {"branches", "tags"}


def workflow_is_triggered(
    workflow: dict[str, Any], event: str, *, ref_type: str = "", ref_name: str = ""
) -> bool:
    """Evaluate the event and ref filters represented in a parsed workflow.

    Push filters follow GitHub's semantics: with neither ``branches`` nor
    ``tags`` configured every push triggers the workflow; configuring only one
    of them restricts the workflow to that ref type, so a ``tags``-only
    workflow never runs for a branch push and a ``branches``-only workflow
    never runs for a tag push. Non-push events are treated as triggered
    whenever they are listed, which over-approximates eligibility and keeps
    the denial tests conservative.
    """
    triggers = workflow.get("on", {})
    if isinstance(triggers, str):
        return triggers == event
    if isinstance(triggers, list):
        return event in triggers
    if event not in triggers:
        return False
    configuration = triggers[event]
    if event != "push" or not isinstance(configuration, dict):
        return True

    unsupported = set(configuration) - PUSH_REF_FILTERS
    assert not unsupported, f"Unsupported push filter keys: {sorted(unsupported)}"
    if not PUSH_REF_FILTERS & set(configuration):
        return True
    filter_name = "tags" if ref_type == "tag" else "branches"
    patterns = configuration.get(filter_name)
    if patterns is None:
        return False
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
        starts_with = re.fullmatch(r"startsWith\(github\.(\w+), '([^']+)'\)", clause)
        if equality:
            results.append(event.get(equality.group(1)) == equality.group(2))
        elif starts_with:
            results.append(event.get(starts_with.group(1), "").startswith(starts_with.group(2)))
        else:
            raise AssertionError(f"Unsupported Orin job guard clause: {clause}")
    return all(results)


def runner_selector_can_match_orin(runner: Any) -> bool:
    """Identify selectors broad enough to select the production Orin runner."""
    labels = set(runner) if isinstance(runner, list) else {runner}
    return bool(
        "self-hosted" in labels or "jetson" in labels or {"linux", "arm64"}.issubset(labels)
    )


def orin_capable_jobs() -> list[tuple[str, str]]:
    """Return every workflow/job pair whose selector could match the Orin."""
    capable: list[tuple[str, str]] = []
    for workflow_path in sorted(WORKFLOW_DIR.glob("*.yml")):
        for job_name, job in load_workflow(workflow_path.name).get("jobs", {}).items():
            if runner_selector_can_match_orin(job.get("runs-on")):
                capable.append((workflow_path.name, job_name))
    return capable


def orin_jobs_for_event(
    event_name: str,
    *,
    ref_type: str = "",
    ref_name: str = "",
    apply_job_guards: bool = True,
) -> list[tuple[str, str]]:
    """Return parsed workflow/job pairs eligible to schedule the production Orin.

    With ``apply_job_guards=False`` only the workflow trigger layer is
    evaluated, which proves the trigger boundary holds without relying on a
    job-level ``if`` guard.
    """
    eligible: list[tuple[str, str]] = []
    event = {
        "event_name": event_name,
        "ref_type": ref_type,
        "ref_name": ref_name,
    }
    for workflow_path in sorted(WORKFLOW_DIR.glob("*.yml")):
        workflow = load_workflow(workflow_path.name)
        if not workflow_is_triggered(workflow, event_name, ref_type=ref_type, ref_name=ref_name):
            continue
        for job_name, job in workflow.get("jobs", {}).items():
            runner = job.get("runs-on")
            if not runner_selector_can_match_orin(runner):
                continue
            if apply_job_guards and not job_guard_allows(job, event):
                continue
            eligible.append((workflow_path.name, job_name))
    return eligible


NON_RELEASE_EVENTS = [
    ("pull_request", "branch", "main"),
    ("push", "branch", "main"),
    ("workflow_dispatch", "branch", "main"),
    ("schedule", "branch", "main"),
]


def test_push_trigger_model_matches_github_ref_filter_semantics() -> None:
    tags_only = {"on": {"push": {"tags": ["v[0-9]+.[0-9]+.[0-9]+"]}}}
    branches_only = {"on": {"push": {"branches": ["main"]}}}
    unfiltered = {"on": {"push": None}}

    assert not workflow_is_triggered(tags_only, "push", ref_type="branch", ref_name="main")
    assert workflow_is_triggered(tags_only, "push", ref_type="tag", ref_name="v1.2.3")
    assert not workflow_is_triggered(tags_only, "push", ref_type="tag", ref_name="v1.2")
    assert not workflow_is_triggered(tags_only, "push", ref_type="tag", ref_name="v1.2.3-rc1")
    assert workflow_is_triggered(branches_only, "push", ref_type="branch", ref_name="main")
    assert not workflow_is_triggered(branches_only, "push", ref_type="tag", ref_name="v1.2.3")
    assert workflow_is_triggered(unfiltered, "push", ref_type="branch", ref_name="main")
    assert workflow_is_triggered(unfiltered, "push", ref_type="tag", ref_name="v1.2.3")


def test_release_detector_is_the_only_orin_capable_job() -> None:
    assert orin_capable_jobs() == [("release.yml", "release-detector")]


def test_release_workflow_triggers_only_on_version_tag_push() -> None:
    release = load_workflow("release.yml")
    assert set(release["on"]) == {"push"}
    for event_name, ref_type, ref_name in NON_RELEASE_EVENTS:
        assert not workflow_is_triggered(
            release, event_name, ref_type=ref_type, ref_name=ref_name
        ), event_name
    assert workflow_is_triggered(release, "push", ref_type="tag", ref_name="v1.2.3")
    assert not workflow_is_triggered(release, "push", ref_type="tag", ref_name="candidate")


@pytest.mark.parametrize(("event_name", "ref_type", "ref_name"), NON_RELEASE_EVENTS)
def test_workflow_triggers_alone_deny_orin_on_non_release_events(
    event_name: str, ref_type: str, ref_name: str
) -> None:
    assert (
        orin_jobs_for_event(
            event_name, ref_type=ref_type, ref_name=ref_name, apply_job_guards=False
        )
        == []
    )


@pytest.mark.parametrize(("event_name", "ref_type", "ref_name"), NON_RELEASE_EVENTS)
def test_non_release_events_cannot_schedule_orin(
    event_name: str, ref_type: str, ref_name: str
) -> None:
    assert orin_jobs_for_event(event_name, ref_type=ref_type, ref_name=ref_name) == []


def test_only_version_tag_push_can_schedule_release_orin() -> None:
    assert orin_jobs_for_event("push", ref_type="tag", ref_name="v1.2.3") == [
        ("release.yml", "release-detector")
    ]
    assert orin_jobs_for_event("push", ref_type="tag", ref_name="candidate") == []
    assert orin_jobs_for_event("workflow_dispatch", ref_type="tag", ref_name="v1.2.3") == []


@pytest.mark.parametrize(
    "selector",
    [
        ["self-hosted"],
        ["jetson"],
        ["linux", "arm64"],
        ["self-hosted", "linux", "arm64", "jetson"],
    ],
)
def test_orin_selector_detection_rejects_broad_subsets(selector: list[str]) -> None:
    assert runner_selector_can_match_orin(selector)


@pytest.mark.parametrize(
    ("event_name", "ref_type", "ref_name"),
    [
        ("pull_request", "branch", "main"),
        ("push", "branch", "main"),
        ("workflow_dispatch", "branch", "main"),
    ],
)
def test_release_orin_job_guard_independently_denies_non_release_events(
    event_name: str, ref_type: str, ref_name: str
) -> None:
    release_job = load_workflow("release.yml")["jobs"]["release-detector"]
    assert not job_guard_allows(
        release_job,
        {"event_name": event_name, "ref_type": ref_type, "ref_name": ref_name},
    )


def test_release_orin_job_guard_allows_version_tag_push() -> None:
    release_job = load_workflow("release.yml")["jobs"]["release-detector"]
    assert job_guard_allows(
        release_job,
        {"event_name": "push", "ref_type": "tag", "ref_name": "v1.2.3"},
    )


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
        "build-off-watchdog",
    }
    assert workflow_is_triggered(build, "pull_request", ref_type="branch", ref_name="main")
    assert workflow_is_triggered(build, "push", ref_type="branch", ref_name="main")
    assert workflow_is_triggered(build, "workflow_dispatch", ref_type="branch", ref_name="main")


def test_cleanup_workflow_is_inert_and_read_only() -> None:
    cleanup = load_workflow("cleanup.yml")
    assert set(cleanup["on"]) == {"pull_request"}
    assert not workflow_is_triggered(cleanup, "schedule", ref_type="branch", ref_name="main")
    assert not workflow_is_triggered(
        cleanup, "workflow_dispatch", ref_type="branch", ref_name="main"
    )
    assert cleanup["permissions"] == {"contents": "read"}
    assert set(cleanup["jobs"]) == {"retired"}
    retired = cleanup["jobs"]["retired"]
    assert retired["if"] == "${{ false }}"
    assert retired["runs-on"] == "ubuntu-latest"
    assert not runner_selector_can_match_orin(retired["runs-on"])
    assert all("docker" not in step.get("run", "") for step in retired["steps"])
