"""Training job UI, nested-result, resume, and durable-log regressions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from config_model import TrainingConfig
from routes import training_jobs


def _failed_job(checkpoint: Path | None = None) -> dict:
    execution = {
        "return_code": -9,
        "signal": "SIGKILL",
        "probable_oom": True,
        "oom_evidence": ["cgroup memory.events recorded an OOM delta"],
        "final_exception": "RuntimeError: complete final exception that must remain readable",
        "final_traceback": "Traceback (most recent call last):\n  frame\nRuntimeError: complete final exception that must remain readable",
        "log_path": "/data/training_workspace/logs/" + "a" * 32 + ".log",
        "memory_peak_bytes": 5 * 1024**3,
        "cgroup_event_delta": {"oom_kill": 1},
        "preflight": {
            "mem_available_bytes": 2 * 1024**3,
            "swap_free_bytes": 1024**3,
            "gpu_lease_holder": "a" * 32,
        },
    }
    train = {"error": execution["final_exception"], "execution": execution}
    if checkpoint:
        train["checkpoint_path"] = str(checkpoint)
    return {
        "id": "a" * 32,
        "type": "prepare_and_train",
        "params": json.dumps({"workers": 4}),
        "status": "failed",
        "result": json.dumps(
            {"prepare": {"exit_code": 0}, "train": train, "error": train["error"]}
        ),
        "execution_metadata": json.dumps(execution),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


def test_nested_prepare_and_train_result_is_flattened() -> None:
    result = {
        "prepare": {"exit_code": 0},
        "train": {"model_path": "/models/pond.pt", "execution": {"log_path": "/logs/a.log"}},
    }
    view = training_jobs._result_view(result)
    assert view["model_path"] == "/models/pond.pt"
    assert view["log_path"] == "/logs/a.log"


def test_structured_training_config_defaults_and_validates_workers() -> None:
    assert TrainingConfig().defaults.workers == 4
    assert TrainingConfig.model_validate({"defaults": {"workers": 0}}).defaults.workers == 0
    with pytest.raises(ValueError):
        TrainingConfig.model_validate({"defaults": {"workers": 5}})


def test_failed_job_page_renders_full_error_evidence_log_and_resume(
    client, monkeypatch, tmp_path: Path
) -> None:
    runs = tmp_path / "runs"
    checkpoint = runs / "train-3" / "weights" / "last.pt"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"last")
    job = _failed_job(checkpoint)
    monkeypatch.setattr(training_jobs, "RUNS_DIR", runs)
    monkeypatch.setattr(training_jobs.db_module, "get_training_jobs", lambda **_kwargs: [job])
    monkeypatch.setattr(training_jobs.db_module, "count_training_jobs", lambda **_kwargs: 1)
    monkeypatch.setattr(training_jobs.db_module, "get_training_uploads", lambda **_kwargs: [])
    response = client.get("/admin/training/jobs")
    assert response.status_code == 200
    body = response.text
    assert "RuntimeError: complete final exception that must remain readable" in body
    assert "Traceback" in body
    assert "Probable resource OOM" in body
    assert "Download durable log" in body
    assert "Resume checkpoint" in body
    assert 'id="terminal-evidence"' in body
    assert 'id="terminal-traceback"' in body
    assert "[:50]" not in body


def test_sparse_final_execution_merges_persisted_evidence() -> None:
    persisted = {
        "command": "python3 train.py --workers 4",
        "preflight": {"mem_available_bytes": 1234},
        "log_path": "/data/training_workspace/logs/a.log",
        "versions": {"torch": "2.4.0"},
    }
    result = {
        "error": "outer failure",
        "execution": {"final_exception": "RuntimeError: outer failure"},
    }
    view = training_jobs._result_view(result, json.dumps(persisted))
    assert view["preflight"]["mem_available_bytes"] == 1234
    assert view["log_path"] == persisted["log_path"]
    assert view["final_exception"] == "RuntimeError: outer failure"


def test_submit_rejects_workers_outside_orin_profile(client) -> None:
    response = client.post(
        "/admin/training/jobs",
        data={"job_type": "train", "params_json": "{}", "workers": "5"},
    )
    assert response.status_code == 400
    assert "0 through 4" in response.json()["error"]


def test_resume_route_rejects_symlink_checkpoint(client, monkeypatch, tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    weights = runs / "train-3" / "weights"
    weights.mkdir(parents=True)
    outside = tmp_path / "outside.pt"
    outside.write_bytes(b"outside")
    linked = weights / "last.pt"
    linked.symlink_to(outside)
    job = _failed_job(linked)
    monkeypatch.setattr(training_jobs, "RUNS_DIR", runs)
    monkeypatch.setattr(training_jobs.db_module, "get_training_job", lambda _job_id: job)
    response = client.post(f"/admin/training/jobs/{job['id']}/resume")
    assert response.status_code == 400
    assert response.json()["error"] == "No valid checkpoint is available"


def test_failed_resume_keeps_valid_source_checkpoint_candidate(monkeypatch, tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    checkpoint = runs / "train-3" / "weights" / "last.pt"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"last")
    job = _failed_job()
    parsed = json.loads(job["result"])
    parsed["train"]["resume_from"] = str(checkpoint)
    job["result"] = json.dumps(parsed)
    monkeypatch.setattr(training_jobs, "RUNS_DIR", runs)
    assert training_jobs._resume_candidate(job) == checkpoint.resolve()


def test_durable_log_route_serves_only_recorded_log(client, monkeypatch, tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / ("a" * 32 + ".log")
    path.write_text("complete durable output\n")
    job = _failed_job()
    parsed = json.loads(job["result"])
    parsed["train"]["execution"]["log_path"] = str(path)
    job["result"] = json.dumps(parsed)
    monkeypatch.setattr(training_jobs, "LOGS_DIR", logs)
    monkeypatch.setattr(training_jobs.db_module, "get_training_job", lambda _job_id: job)
    response = client.get(f"/admin/training/jobs/{job['id']}/log")
    assert response.status_code == 200
    assert response.text == "complete durable output\n"
