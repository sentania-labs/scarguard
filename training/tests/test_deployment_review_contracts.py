"""Regression contracts for deployment and release review fixes."""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("existing", [None, ""])
def test_documented_upgrade_backfills_controller_token(
    existing: str | None, tmp_path: Path
) -> None:
    readme = (REPO_ROOT / "README.md").read_text()
    upgrade = readme.split("### Upgrading from a previous version", 1)[1].split("---", 1)[0]
    setup = (REPO_ROOT / "setup.sh").read_text()
    assert "sudo bash setup.sh" in upgrade
    assert "bash infra/backfill-training-controller-token.sh .env" in setup

    env_file = tmp_path / ".env"
    contents = "REDIS_PASSWORD=test-redis-password\n"
    if existing is not None:
        contents += f"TRAINING_CONTROLLER_TOKEN={existing}\n"
    env_file.write_text(contents)

    subprocess.run(
        ["bash", "infra/backfill-training-controller-token.sh", str(env_file)],
        cwd=REPO_ROOT,
        check=True,
    )
    values = dict(
        line.split("=", 1)
        for line in env_file.read_text().splitlines()
        if line and not line.startswith("#")
    )
    token = values["TRAINING_CONTROLLER_TOKEN"]
    assert len(token) >= 32

def test_training_controller_mounts_the_shared_config() -> None:
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    controller = compose.split("  training-controller:", 1)[1].split("  trainer:", 1)[0]

    assert "CONFIG_PATH: /config/scarguard.yml" in controller
    assert "scarguard-config:/config:ro" in controller


def test_trainer_release_uses_the_tag_commit_date() -> None:
    release = (REPO_ROOT / ".github/workflows/release.yml").read_text()
    trainer = release.split("  release-trainer:", 1)[1].split("  release-detector-x86:", 1)[0]

    assert 'BUILD_DATE="$(git show -s --format=%cI "$GITHUB_SHA")"' in trainer
    assert '--build-arg BUILD_DATE="$BUILD_DATE"' in trainer
    assert "github.event.release.published_at" not in trainer
