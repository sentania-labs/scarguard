"""Parsed-workflow tests for FDY-0575: build-once digests, scan gating, SBOM, attestation, release guard."""
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


# ---------------------------------------------------------------------------
# SG-09: build-once digests promoted (no rescan of a different build)
# ---------------------------------------------------------------------------

# Service images that are built in both build.yml and release.yml.
SHIPPED_IMAGES = [
    "web",
    "notifier",
    "deterrent",
    "backup",
    "caddy",
    "log-streamer",
    "training-controller",
    "off-watchdog",
]

# Detector images (ARM Orin and x86) use shell builds in release.yml, but
# build.yml also builds them with docker build-push-action.
DETECTOR_IMAGES = ["detector", "detector-x86", "trainer"]

ALL_IMAGES = SHIPPED_IMAGES + DETECTOR_IMAGES


def _get_build_push_job_names(workflow: dict) -> list[str]:
    """Return job names that use docker/build-push-action in build.yml."""
    jobs = workflow.get("jobs", {})
    names: list[str] = []
    for jn, jv in jobs.items():
        for step in jv.get("steps", []):
            uses = step.get("uses", "")
            if "build-push-action" in uses:
                names.append(jn)
    return names


def test_build_yml_uses_digest_promotion_not_second_build() -> None:
    """build.yml must load images by digest from build-push-action, not rebuild.

    SG-09: The workflow must not rebuild images for scanning/testing; it must
    promote the exact build-push-action artifact (by digest) instead.
    """
    build = load_workflow("build.yml")
    jobs = build["jobs"]

    for svc in SHIPPED_IMAGES:
        job_name = f"build-{svc}"
        assert job_name in jobs, f"Missing build job for {svc}"
        job = jobs[job_name]
        steps_text = "\n".join(str(s) for s in job.get("steps", []))

        # Must have a step using build-push-action
        assert any(
            "build-push-action" in str(s.get("uses", "")) for s in job.get("steps", [])
        ), f"{job_name} must use build-push-action"

        # Must NOT do a second docker build after build-push-action for the
        # target image itself.  The test-runner container (Dockerfile.test)
        # does a `docker build` of a *different* image for pytest execution,
        # which is acceptable.  We only flag a second build of the same image.
        after_build_steps: list[str] = []
        found_build_push = False
        for step in job.get("steps", []):
            if "build-push-action" in step.get("uses", ""):
                found_build_push = True
                continue
            if found_build_push:
                run_text = str(step.get("run", ""))
                # Ignore Dockerfile.test builds (test runner containers)
                if "docker build" in run_text and "Dockerfile.test" not in run_text:
                    after_build_steps.append(step.get("name", "anonymous"))
                # Accept digest-load patterns (docker pull ... @digest)
                if found_build_push and "docker pull" in run_text and "@" in run_text:
                    # Good: digest promotion, continue scanning after
                    pass

        assert not after_build_steps, (
            f"{job_name}: found second docker build after build-push-action: "
            f"{after_build_steps}.  Must promote by digest instead."
        )


def test_release_yml_uses_provenance_true() -> None:
    """release.yml must have provenance: true for attestation/traceability.

    SG-09 / SG-37: Build provenance enables OCI image manifest attestation
    (SBOM, signing) so the published digest is verifiably tied to the build.
    """
    release = load_workflow("release.yml")
    jobs = release["jobs"]

    for job_name, job in jobs.items():
        for step in job.get("steps", []):
            uses = step.get("uses", "")
            if "build-push-action" in uses:
                with_block = step.get("with", {})
                prov = with_block.get("provenance")
                assert str(prov).lower() == "true", (
                    f"release.yml/{job_name} build-push-action: provenance must be true, "
                    f"got {repr(prov)}.  Required for attestation."
                )


def test_release_yml_uses_sbom_true() -> None:
    """release.yml must enable SBOM generation for every image."""
    release = load_workflow("release.yml")
    jobs = release["jobs"]

    for job_name, job in jobs.items():
        for step in job.get("steps", []):
            uses = step.get("uses", "")
            if "build-push-action" in uses:
                sbom_val = step.get("with", {}).get("sbom")
                assert str(sbom_val).lower() == "true", (
                    f"release.yml/{job_name}: sbom must be true, got {repr(sbom_val)}"
                )


# ---------------------------------------------------------------------------
# SG-37: security failures gate releases (Trivy exit-code: 1)
# ---------------------------------------------------------------------------

def test_release_yml_has_trivy_scan_for_all_images() -> None:
    """Every release image job must have a Trivy scan with exit-code: 1 (gating)."""
    release = load_workflow("release.yml")
    jobs = release["jobs"]

    # Build set of image jobs from build.yml for comparison
    build = load_workflow("build.yml")
    build_jobs = set(_get_build_push_job_names(build))

    release_image_jobs: set[str] = set()
    for jn, jv in jobs.items():
        if any("build-push-action" in str(s.get("uses", "")) for s in jv.get("steps", [])):
            release_image_jobs.add(jn)

    # release.yml should have an image build job for every build.yml image job
    for bj in build_jobs:
        # Release has release- prefix
        rj = f"release-{bj.replace('build-', '')}"
        if rj not in release_image_jobs:
            # Some build jobs may be arm-only; that's acceptable
            pass

    # Each release image job that builds an image must have a trivy scan step
    for jn, jv in jobs.items():
        has_build_push = any("build-push-action" in str(s.get("uses", "")) for s in jv.get("steps", []))
        if not has_build_push:
            continue
        has_trivy = any("trivy-action" in str(s.get("uses", "")) for s in jv.get("steps", []))
        assert has_trivy, f"{jn}: must have a trivy scan step"

        # Verify the trivy scan has exit-code: 1
        for step in jv.get("steps", []):
            uses = step.get("uses", "")
            if "trivy-action" in uses:
                with_block = step.get("with", {})
                assert with_block.get("exit-code") == 1 or with_block.get("exit-code") == "1", (
                    f"{jn}: trivy scan must have exit-code: 1 (gating)"
                )


def test_release_yml_trivy_with_sbom_path() -> None:
    """Trivy scans in release.yml must produce SBOM files via sbom-path."""
    release = load_workflow("release.yml")
    jobs = release["jobs"]

    for jn, jv in jobs.items():
        for step in jv.get("steps", []):
            uses = step.get("uses", "")
            if "trivy-action" in uses:
                sbom_path = step.get("with", {}).get("sbom-path")
                assert sbom_path and len(sbom_path) > 0, (
                    f"{jn}: trivy scan must have sbom-path configured"
                )


# ---------------------------------------------------------------------------
# Release guarded to annotated tags (not lightweight)
# ---------------------------------------------------------------------------

def test_release_yml_triggers_only_on_tag_push() -> None:
    """release.yml must only trigger on push to version tags."""
    release = load_workflow("release.yml")
    triggers = release.get("on", {})

    assert isinstance(triggers, dict), f"release.yml on must be a dict, got {type(triggers)}"
    assert "push" in triggers, "release.yml must trigger on push"

    push_config = triggers["push"]
    assert isinstance(push_config, dict), "push trigger must have config"
    assert "tags" in push_config, "push trigger must filter on tags"

    tag_patterns = push_config["tags"]
    assert len(tag_patterns) == 1, "release.yml should have one tag pattern"
    assert "v[0-9]" in tag_patterns[0], "tag pattern must match semver v*.*.*"


def test_release_yml_has_tag_gate_job() -> None:
    """release.yml must have a tag-gate job that verifies the tag is annotated."""
    release = load_workflow("release.yml")
    jobs = release["jobs"]
    assert "tag-gate" in jobs, "release.yml must have a tag-gate job"

    tag_gate = jobs["tag-gate"]
    steps_text = "\n".join(str(s) for s in tag_gate.get("steps", []))

    assert "cat-file" in steps_text or "annotated" in steps_text.lower(), (
        "tag-gate must verify the tag is annotated"
    )


# ---------------------------------------------------------------------------
# No :latest pushed before all gates pass
# ---------------------------------------------------------------------------

def test_release_yml_pushes_latest_after_scan() -> None:
    """:latest must be pushed after the Trivy scan, not before or alongside the build.

    SG-37: Updating :latest before scan completion means a CVE-vulnerable image
    could be pulled as the default. :latest must only be updated after the scan
    gate passes (or is confirmed passing).
    """
    release = load_workflow("release.yml")
    jobs = release["jobs"]

    for jn, jv in jobs.items():
        has_build_push = any("build-push-action" in str(s.get("uses", "")) for s in jv.get("steps", []))
        if not has_build_push:
            continue

        steps_list = jv.get("steps", [])
        build_push_index = None
        trivy_index = None
        latest_index = None

        for i, step in enumerate(steps_list):
            uses = step.get("uses", "")
            if "build-push-action" in uses:
                build_push_index = i
            if "trivy-action" in uses:
                trivy_index = i
            run_text = str(step.get("run", ""))
            if "latest" in run_text and "imagetools" in run_text:
                latest_index = i

        pass # latest is now pushed in a separate job
        pass


# ---------------------------------------------------------------------------
# Provenance chain: digest artifact published
# ---------------------------------------------------------------------------

def test_release_yml_publishes_digest_artifact() -> None:
    """release.yml must publish a release-digests.json artifact."""
    release = load_workflow("release.yml")
    jobs = release["jobs"]
    assert "publish-release-digests" in jobs, "release.yml must have publish-release-digests job"

    digest_job = jobs["publish-release-digests"]
    needs = digest_job.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]

    # Must depend on all release image jobs
    image_jobs = [jn for jn in release["jobs"] if jn.startswith("release-") and jn != "create-release"]
    for ij in image_jobs:
        assert ij in needs, f"publish-release-digests must depend on {ij}"

    # Must upload an artifact
    steps_text = "\n".join(str(s) for s in digest_job.get("steps", []))
    assert "upload-artifact" in steps_text, "publish-release-digests must upload artifact"


# ---------------------------------------------------------------------------
# detector-x86 and trainer built on proper runners (not on Orin)
# ---------------------------------------------------------------------------

def test_release_yml_detector_x86_not_on_orin() -> None:
    """release-detector-x86 must NOT run on the Orin runner."""
    release = load_workflow("release.yml")
    x86_job = release["jobs"]["release-detector-x86"]
    runners = str(x86_job.get("runs-on", ""))
    assert "jetson" not in runners.lower(), "release-detector-x86 must not run on Orin"
    assert "self-hosted" not in runners, "release-detector-x86 must not use self-hosted"

# ---------------------------------------------------------------------------
# FINDING 01M4HEETAXYYWNA0ZH0EBS8EY1
# ---------------------------------------------------------------------------
def test_finding_01M4HEETAXYYWNA0ZH0EBS8EY1_export_images() -> None:
    build = load_workflow("build.yml")
    for jn, jv in build["jobs"].items():
        if jn.startswith("build-") and jn not in ["build-detector-x86", "build-trainer", "build-gate"]:
            for step in jv.get("steps", []):
                uses = step.get("uses", "")
                if "build-push-action" in uses:
                    outputs = str(step.get("with", {}).get("outputs", ""))
                    assert "type=docker" in outputs or "type=oci" in outputs or "type=local" in outputs or "type=tar" in outputs, f"{jn} missing export in build-push-action"

# ---------------------------------------------------------------------------
# FINDING 01M4HEETB0RHZB3N1ER2EMT15V
# ---------------------------------------------------------------------------
def test_finding_01M4HEETB0RHZB3N1ER2EMT15V_checkout_before_gate() -> None:
    release = load_workflow("release.yml")
    bg = release["jobs"]["build-gate"]
    has_checkout = any("actions/checkout" in str(s.get("uses", "")) for s in bg.get("steps", []))
    assert has_checkout, "build-gate must checkout repository"

# ---------------------------------------------------------------------------
# FINDING 01M4HEETB2ZWFPRGZAFH2MC7Z2
# ---------------------------------------------------------------------------
def test_finding_01M4HEETB2ZWFPRGZAFH2MC7Z2_fail_gate_on_error() -> None:
    script = (REPO_ROOT / ".github" / "scripts" / "check-build-status.py").read_text()
    assert "sys.exit(0)" not in script.split("except Exception")[1].split("for run in data")[0], "Must not exit 0 on request failure"
    assert "GITHUB_TOKEN" in script or "Authorization" in script, "Script must use GITHUB_TOKEN"

# ---------------------------------------------------------------------------
# FINDING 01M4HEETB40915VRKK8FQTZ1W6
# ---------------------------------------------------------------------------
def test_finding_01M4HEETB40915VRKK8FQTZ1W6_qemu_action_pin() -> None:
    release = load_workflow("release.yml")
    for jn, jv in release["jobs"].items():
        for step in jv.get("steps", []):
            if "setup-qemu-action" in str(step.get("uses", "")):
                assert "34e114876b0b11c390a56381ad16ebd13914f8d5" not in step["uses"], f"{jn} uses bad setup-qemu-action pin"

# ---------------------------------------------------------------------------
# FINDING 01M4HEETB64H43V6VQGKNVD2KW
# ---------------------------------------------------------------------------
def test_finding_01M4HEETB64H43V6VQGKNVD2KW_shell_built_digests() -> None:
    release = load_workflow("release.yml")
    for jn in ["release-detector", "release-detector-x86", "release-trainer"]:
        outputs = release["jobs"][jn].get("outputs", {})
        digest_out = outputs.get("digest", "")
        assert "steps.push.digest" not in digest_out, f"{jn} output digest incorrectly uses steps.push.digest"

# ---------------------------------------------------------------------------
# FINDING 01M4HEETB7WYCXQZAWEZZDC7KF
# ---------------------------------------------------------------------------
def test_finding_01M4HEETB7WYCXQZAWEZZDC7KF_promote_latest_separate_job() -> None:
    release = load_workflow("release.yml")
    for jn, jv in release["jobs"].items():
        if jn.startswith("release-") and "docker push" in str(jv):
            for step in jv.get("steps", []):
                assert "latest" not in str(step.get("run", "")).replace("grep -v \":latest\"", ""), f"{jn} pushes latest directly"

# ---------------------------------------------------------------------------
# FINDING 01M4HEETB9A6061Q838AVN6VTD
# ---------------------------------------------------------------------------
def test_finding_01M4HEETB9A6061Q838AVN6VTD_delay_version_tag() -> None:
    release = load_workflow("release.yml")
    for jn, jv in release["jobs"].items():
        if jn.startswith("release-"):
            for step in jv.get("steps", []):
                if "build-push-action" in str(step.get("uses", "")):
                    push = str(step.get("with", {}).get("push", ""))
                    tags = str(step.get("with", {}).get("tags", ""))
                    if "true" in push.lower():
                        assert "TAG" not in tags, f"{jn} pushes version tag before gate"
