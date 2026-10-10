"""FDY-0570 (SG-35): the Roboflow key reaches prepare_dataset via a 0600 file, never argv."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import job_runner

# Stand-in for scripts/prepare_dataset.py, run by the real _run_subprocess:
# it records its own kernel-visible argv and the key file's mode and content
# match (never the content itself).
_STUB = """
import json, os, stat, sys
from pathlib import Path
argv = Path("/proc/self/cmdline").read_bytes().split(b"\\0")
key_path = sys.argv[sys.argv.index("--roboflow-key-file") + 1]
expected = os.environ.get("FDY0570_EXPECTED", "")
report = {
    "argv_has_key": any(expected.encode() in a for a in argv),
    "mode": stat.S_IMODE(os.stat(key_path).st_mode),
    "content_matches": Path(key_path).read_text() == expected,
    "key_path": key_path,
}
Path(os.environ["FDY0570_REPORT"]).write_text(json.dumps(report))
"""


class FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list] = {}

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def ltrim(self, *_args):
        pass

    def expire(self, *_args):
        pass

    def publish(self, *_args):
        pass

    def set(self, *_args, **_kwargs):
        return True

    def get(self, _key):
        return None


def _snapshot(_ctx):
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


def test_roboflow_key_is_a_private_file_not_argv(tmp_path: Path, monkeypatch) -> None:
    key = f"sentinel{uuid.uuid4().hex}"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "prepare_dataset.py").write_text(_STUB)
    report = tmp_path / "report.json"
    monkeypatch.setenv("FDY0570_EXPECTED", key)
    monkeypatch.setenv("FDY0570_REPORT", str(report))
    monkeypatch.setattr(job_runner, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(job_runner, "WORKSPACE_DIR", tmp_path / "workspace")
    monkeypatch.setattr(job_runner, "make_sync_client", lambda _cfg: FakeRedis())
    monkeypatch.setattr(job_runner, "DetectorControllerClient", lambda _job: SimpleNamespace())
    monkeypatch.setattr(job_runner, "_resource_snapshot", _snapshot)

    job = {"id": "c" * 32, "type": "prepare_dataset", "params": json.dumps({"skip_oid": True})}
    cfg = {"training": {"sources": {"roboflow": {"api_key": key}}}}
    ctx = job_runner.JobContext(job, cfg, SimpleNamespace(is_set=lambda: False))
    executions: list[dict] = []
    monkeypatch.setattr(ctx, "persist_execution", executions.append)

    result = job_runner._run_prepare_dataset(ctx)
    ctx.close()

    assert "error" not in result
    seen = json.loads(report.read_text())
    assert seen["argv_has_key"] is False
    assert seen["mode"] == 0o600
    assert seen["content_matches"] is True
    # The key file does not outlive the step, and no record holds the key.
    assert not Path(seen["key_path"]).exists()
    assert key not in ctx.log_path.read_text()
    assert key not in json.dumps(executions)
