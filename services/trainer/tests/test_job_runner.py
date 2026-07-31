"""Training execution regressions derived from the July failure reproduction."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import job_runner
import pytest


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    def set(self, key, value, **kwargs):
        if kwargs.get("nx") and key in self.values:
            return None
        if kwargs.get("xx") and key not in self.values:
            return False
        self.values[key] = str(value)
        return True

    def get(self, key):
        return self.values.get(key)

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def ltrim(self, key, start, _end):
        self.lists[key] = self.lists.get(key, [])[start:]

    def expire(self, *_args):
        return True

    def incr(self, key):
        self.values[key] = str(int(self.values.get(key, "0")) + 1)
        return int(self.values[key])

    def exists(self, key):
        return key in self.values

    def eval(self, script, _keys, key, owner, *_args):
        if self.values.get(key) == owner:
            if "expire" in script:
                return 1
            del self.values[key]
            return 1
        return 0

    def close(self):
        pass


class FakeContext:
    def __init__(self, workspace: Path, params: dict | None = None) -> None:
        self.params = params or {}
        self.log_path = workspace / "logs" / "job.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.progress: list[tuple] = []
        self.logs: list[str] = []
        self.execution: list[dict] = []
        self.release_count = 0

    def training_config(self):
        return {"defaults": {"workers": 4}, "resources": {"min_mem_available_mb": 512}}

    def publish_progress(self, *args, **kwargs):
        self.progress.append((args, kwargs))

    def append_log(self, line):
        self.logs.append(str(line))

    def sanitize(self, text):
        return str(text)

    def gpu_lease_holder(self):
        return "a" * 32

    def persist_execution(self, value):
        self.execution.append(json.loads(json.dumps(value)))

    def is_cancelled(self):
        return False

    def gpu_lease_lost(self):
        return False

    def acquire_detector(self):
        return {"stopped_by_controller": True}

    def release_detector(self):
        self.release_count += 1
        return {"restored": True}


def abundant_snapshot(_ctx):
    return {
        "timestamp": "now",
        "mem_available_bytes": 4 * 1024**3,
        "swap_free_bytes": 2 * 1024**3,
        "swap_total_bytes": 4 * 1024**3,
        "cgroup_current_bytes": 100,
        "cgroup_peak_bytes": 200,
        "cgroup_events": {"oom": 0, "oom_kill": 0},
        "gpu_lease_holder": "a" * 32,
    }


def test_exact_fresh_command_and_default_workers(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    data_yaml = workspace / "merged_dataset" / "data.yaml"
    data_yaml.parent.mkdir(parents=True)
    data_yaml.write_text("names: []\n")
    models = tmp_path / "models"
    models.mkdir()
    (models / "yolov8n.pt").write_bytes(b"model")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setattr(job_runner, "WORKSPACE_DIR", workspace)
    monkeypatch.setattr(job_runner, "MODELS_DIR", str(models))
    monkeypatch.setattr(job_runner, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(job_runner, "_resource_snapshot", abundant_snapshot)
    monkeypatch.setattr(job_runner, "_newest_checkpoint_since", lambda _started: None)
    captured = {}

    def fake_run(_ctx, cmd, phase, *, preflight=None, admission=None):
        captured.update(cmd=cmd, phase=phase, preflight=preflight, admission=admission)
        return {"phase": phase, "execution": {}}

    monkeypatch.setattr(job_runner, "_run_subprocess", fake_run)
    ctx = FakeContext(workspace)
    result = job_runner._run_train(ctx)

    assert captured["cmd"] == [
        "python3",
        str(scripts / "train.py"),
        "--data",
        str(data_yaml),
        "--base-model",
        str(models / "yolov8n.pt"),
        "--output",
        str(models / "trained.pt"),
        "--project",
        str(workspace / "runs"),
        "--epochs",
        "100",
        "--imgsz",
        "480",
        "--batch",
        "2",
        "--patience",
        "20",
        "--device",
        "0",
        "--workers",
        "4",
    ]
    assert result["model_path"] == str(models / "trained.pt")
    assert ctx.release_count == 1
    assert captured["admission"]["admitted"] is True
    assert captured["admission"]["detector_lease"] == {"stopped_by_controller": True}
    assert captured["admission"]["thresholds"]["min_mem_available_bytes"] == 512 * 1024 * 1024


@pytest.mark.parametrize(
    "subprocess_result", [{}, {"error": "boom"}, {"error": "Job cancelled", "cancelled": True}]
)
def test_detector_restored_on_every_train_result(
    tmp_path: Path, monkeypatch, subprocess_result: dict
) -> None:
    workspace = tmp_path / "workspace"
    data_yaml = workspace / "merged_dataset" / "data.yaml"
    data_yaml.parent.mkdir(parents=True)
    data_yaml.write_text("names: []\n")
    monkeypatch.setattr(job_runner, "WORKSPACE_DIR", workspace)
    monkeypatch.setattr(job_runner, "MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(job_runner, "_resource_snapshot", abundant_snapshot)
    monkeypatch.setattr(job_runner, "_newest_checkpoint_since", lambda _started: None)
    monkeypatch.setattr(
        job_runner,
        "_run_subprocess",
        lambda *_args, **_kwargs: {**subprocess_result, "execution": {}},
    )
    ctx = FakeContext(workspace)
    job_runner._run_train(ctx)
    assert ctx.release_count == 1


def test_durable_log_complete_redis_capped_and_tail_bounded(tmp_path: Path, monkeypatch) -> None:
    fake_redis = FakeRedis()
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(job_runner, "WORKSPACE_DIR", workspace)
    monkeypatch.setattr(job_runner, "make_sync_client", lambda _cfg: fake_redis)
    monkeypatch.setattr(job_runner, "DetectorControllerClient", lambda _job: SimpleNamespace())
    monkeypatch.setattr(job_runner, "_resource_snapshot", abundant_snapshot)
    job = {"id": "a" * 32, "type": "train", "params": "{}"}
    cfg = {
        "training": {
            "logs": {"max_bytes": 1024 * 1024},
            "sources": {"roboflow": {"api_key": "super-secret-value"}},
        }
    }
    ctx = job_runner.JobContext(job, cfg, SimpleNamespace(is_set=lambda: False))
    monkeypatch.setattr(ctx, "persist_execution", lambda _value: None)
    ctx.append_log("credential=super-secret-value")
    command = [
        sys.executable,
        "-c",
        "for i in range(700):\n    print(f'line-{i}')\n"
        "print('Traceback (most recent call last):')\nprint('  fake frame')\n"
        "print('RuntimeError: final failure')\nraise SystemExit(1)",
    ]
    result = job_runner._run_subprocess(ctx, command, "train", preflight=abundant_snapshot(ctx))
    ctx.close()

    durable = ctx.log_path.read_text()
    assert "line-0" in durable and "line-699" in durable
    assert "RuntimeError: final failure" in durable
    assert "super-secret-value" not in durable
    assert len(fake_redis.lists[job_runner._log_key(job["id"])]) == 500
    assert result["execution"]["line_count"] == 703
    assert len(result["execution"]["tail"]) == 200
    assert result["execution"]["final_exception"] == "RuntimeError: final failure"
    assert result["execution"]["memory_peak_bytes"] == 100
    assert result["execution"]["resources"]["cgroup_reported_peak_baseline_bytes"] == 200


def test_signal_decoding_and_evidence_based_oom() -> None:
    assert job_runner._decode_return_code(1) == (1, None)
    assert job_runner._decode_return_code(-9) == (None, "SIGKILL")
    assert job_runner._decode_return_code(-15) == (None, "SIGTERM")
    assert job_runner._probable_oom("SIGKILL", {"cgroup_event_delta": {}}) == (False, [])
    probable, reasons = job_runner._probable_oom(
        "SIGKILL",
        {"cgroup_event_delta": {"oom_kill": 1}, "last_sample": {}},
    )
    assert probable is True
    assert reasons
    assert job_runner._probable_oom("SIGTERM", {"cgroup_event_delta": {"oom_kill": 1}}) == (
        False,
        [],
    )


def test_gpu_lease_does_not_overwrite_foreign_holder(tmp_path: Path, monkeypatch) -> None:
    fake_redis = FakeRedis()
    fake_redis.values[job_runner.HEARTBEAT_KEY] = "ci-123"
    controller = SimpleNamespace(acquire=lambda: pytest.fail("controller must not be called"))
    monkeypatch.setattr(job_runner, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(job_runner, "make_sync_client", lambda _cfg: fake_redis)
    monkeypatch.setattr(job_runner, "DetectorControllerClient", lambda _job: controller)
    job = {"id": "a" * 32, "type": "train", "params": "{}"}
    ctx = job_runner.JobContext(job, {}, SimpleNamespace(is_set=lambda: False))
    with pytest.raises(RuntimeError, match="ci-123"):
        ctx.acquire_detector()
    ctx.close()


def test_gpu_lease_refresh_is_atomic_and_fails_closed(tmp_path: Path, monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr(job_runner, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(job_runner, "make_sync_client", lambda _cfg: fake_redis)
    monkeypatch.setattr(job_runner, "DetectorControllerClient", lambda _job: SimpleNamespace())
    job = {"id": "a" * 32, "type": "train", "params": "{}"}
    ctx = job_runner.JobContext(job, {}, SimpleNamespace(is_set=lambda: False))

    fake_redis.values[job_runner.HEARTBEAT_KEY] = job["id"]
    assert ctx._refresh_gpu_lease() is True
    fake_redis.values[job_runner.HEARTBEAT_KEY] = "ci-foreign"
    assert ctx._refresh_gpu_lease() is False
    ctx._lease_lost.set()
    assert ctx.is_cancelled() is True
    ctx.close()


def test_gpu_lease_loss_terminates_running_child(tmp_path: Path, monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr(job_runner, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(job_runner, "make_sync_client", lambda _cfg: fake_redis)
    monkeypatch.setattr(job_runner, "DetectorControllerClient", lambda _job: SimpleNamespace())
    monkeypatch.setattr(job_runner, "_resource_snapshot", abundant_snapshot)
    job = {"id": "a" * 32, "type": "train", "params": "{}"}
    ctx = job_runner.JobContext(job, {}, SimpleNamespace(is_set=lambda: False))
    monkeypatch.setattr(ctx, "persist_execution", lambda _value: None)
    ctx._lease_lost.set()
    result = job_runner._run_subprocess(
        ctx,
        [sys.executable, "-c", "import time; time.sleep(60)"],
        "train",
        preflight=abundant_snapshot(ctx),
    )
    ctx.close()
    assert result["error"] == "GPU lease ownership was lost; training was stopped"
    assert "cancelled" not in result


def test_redis_live_tail_failure_does_not_break_durable_log(tmp_path: Path, monkeypatch) -> None:
    class BrokenLiveRedis(FakeRedis):
        def rpush(self, _key, _value):
            raise ConnectionError("live Redis unavailable")

        def set(self, _key, _value, **_kwargs):
            raise ConnectionError("live Redis unavailable")

    monkeypatch.setattr(job_runner, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(job_runner, "make_sync_client", lambda _cfg: BrokenLiveRedis())
    monkeypatch.setattr(job_runner, "DetectorControllerClient", lambda _job: SimpleNamespace())
    job = {"id": "a" * 32, "type": "train", "params": "{}"}
    ctx = job_runner.JobContext(job, {}, SimpleNamespace(is_set=lambda: False))
    ctx.publish_progress("train", 1, "still durable")
    ctx.append_log("durable despite Redis")
    ctx.close()
    assert "durable despite Redis" in ctx.log_path.read_text()


def test_durable_log_rejects_preexisting_symlink(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    logs = workspace / "logs"
    logs.mkdir(parents=True)
    outside = tmp_path / "outside.log"
    outside.write_text("do not overwrite\n")
    (logs / ("a" * 32 + ".log")).symlink_to(outside)
    monkeypatch.setattr(job_runner, "WORKSPACE_DIR", workspace)
    monkeypatch.setattr(job_runner, "make_sync_client", lambda _cfg: FakeRedis())
    monkeypatch.setattr(job_runner, "DetectorControllerClient", lambda _job: SimpleNamespace())
    job = {"id": "a" * 32, "type": "train", "params": "{}"}
    with pytest.raises(OSError):
        job_runner.JobContext(job, {}, SimpleNamespace(is_set=lambda: False))
    assert outside.read_text() == "do not overwrite\n"


def test_cancellation_terminates_process_group(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(job_runner, "WORKSPACE_DIR", workspace)
    monkeypatch.setattr(job_runner, "_resource_snapshot", abundant_snapshot)
    ctx = FakeContext(workspace)
    child_pid = 0

    def cancelled() -> bool:
        nonlocal child_pid
        if ctx.logs and ctx.logs[-1].isdigit():
            child_pid = int(ctx.logs[-1])
            return True
        return False

    ctx.is_cancelled = cancelled  # type: ignore[method-assign]
    code = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "print(p.pid, flush=True); time.sleep(60)"
    )
    result = job_runner._run_subprocess(
        ctx, [sys.executable, "-c", code], "train", preflight=abundant_snapshot(ctx)
    )
    assert result["cancelled"] is True
    assert child_pid > 0
    deadline = time.time() + 3
    while Path(f"/proc/{child_pid}").exists() and time.time() < deadline:
        time.sleep(0.05)
    assert not Path(f"/proc/{child_pid}").exists()


def _proc_alive(pid: int) -> bool:
    """True while pid exists and is not a zombie awaiting reaping."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(") ", 1)[-1][:1] != "Z"


def test_exited_leader_does_not_leave_stdout_holding_child(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(job_runner, "WORKSPACE_DIR", workspace)
    monkeypatch.setattr(job_runner, "_resource_snapshot", abundant_snapshot)
    ctx = FakeContext(workspace)
    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    parent_code = (
        "import subprocess,sys; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        "print(p.pid, flush=True); raise SystemExit(1)"
    )
    started = time.monotonic()
    result = job_runner._run_subprocess(
        ctx, [sys.executable, "-c", parent_code], "train", preflight=abundant_snapshot(ctx)
    )
    elapsed = time.monotonic() - started
    child_pid = int(ctx.logs[-1])
    assert result["execution"]["return_code"] == 1
    assert elapsed < 5
    # The SIGKILLed grandchild may linger briefly as an unreaped zombie under
    # /proc until init reaps it; poll and treat zombie state as terminated.
    deadline = time.time() + 3
    while _proc_alive(child_pid) and time.time() < deadline:
        time.sleep(0.05)
    assert not _proc_alive(child_pid)


def test_admission_evidence_survives_final_execution_persist(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(job_runner, "WORKSPACE_DIR", workspace)
    monkeypatch.setattr(job_runner, "_resource_snapshot", abundant_snapshot)
    ctx = FakeContext(workspace)
    admission = {
        "admitted": True,
        "thresholds": {"min_mem_available_bytes": 512 * 1024 * 1024},
        "detector_lease": {"stopped_by_controller": True},
    }
    result = job_runner._run_subprocess(
        ctx,
        [sys.executable, "-c", "print('ok')"],
        "train",
        preflight=abundant_snapshot(ctx),
        admission=admission,
    )
    assert result["execution"]["admission"] == admission
    assert ctx.execution[-1]["admission"] == admission
    assert ctx.execution[-1]["return_code"] == 0


def test_cancelled_process_video_reports_cancelled_status(tmp_path: Path) -> None:
    class CancelledContext(FakeContext):
        def detection_config(self):
            return {}

        def is_cancelled(self):
            return True

    ctx = CancelledContext(tmp_path / "workspace", params={"upload_ids": ["u1"]})
    result = job_runner._run_process_video(ctx)
    assert result["error"] == "Job cancelled"
    assert result["cancelled"] is True
    assert ctx.release_count == 1


def test_signal_failure_is_the_structured_final_exception(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(job_runner, "WORKSPACE_DIR", workspace)
    monkeypatch.setattr(job_runner, "_resource_snapshot", abundant_snapshot)
    ctx = FakeContext(workspace)
    result = job_runner._run_subprocess(
        ctx,
        [
            sys.executable,
            "-c",
            "import os,signal; print('ordinary progress', flush=True); os.kill(os.getpid(), signal.SIGKILL)",
        ],
        "train",
        preflight=abundant_snapshot(ctx),
    )
    assert result["error"] == "train terminated by SIGKILL"
    assert result["execution"]["final_exception"] == "train terminated by SIGKILL"
    assert result["execution"]["last_output_line"] == "ordinary progress"


def test_newline_free_output_is_read_in_bounded_records(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(job_runner, "WORKSPACE_DIR", workspace)
    monkeypatch.setattr(job_runner, "_resource_snapshot", abundant_snapshot)
    ctx = FakeContext(workspace)
    result = job_runner._run_subprocess(
        ctx,
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 100000)"],
        "train",
        preflight=abundant_snapshot(ctx),
    )
    assert result["execution"]["line_count"] > 1
    assert max(map(len, ctx.logs)) < 17_000
    assert any("line continues" in line for line in ctx.logs)
