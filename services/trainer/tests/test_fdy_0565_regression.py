"""FDY-0565 / SG-07: training and uploads create candidates, never the live model.

Exercises the real trainer handler (``job_runner._run_train`` driving the real
``_run_subprocess`` process-group runner) with a CPU-only stand-in for
``train.py``, and the real ``model_store`` publication, promotion and rollback
code against files on disk.  No GPU, Redis or detector is involved.
"""

from __future__ import annotations

import io
import json
import os
import pickle
import sys
import textwrap
import zipfile
from collections import OrderedDict
from pathlib import Path

import pytest


def _layout_root() -> Path:
    """Directory holding ``shared/`` - repo root in a checkout, /app in an image."""
    for ancestor in Path(__file__).resolve().parents:
        if (ancestor / "shared" / "training_safety.py").is_file():
            return ancestor
    raise RuntimeError("shared/ helpers not found above the test file")


_SRC = Path(__file__).resolve().parent.parent / "src"
for _path in (_layout_root() / "shared", _SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import job_runner  # noqa: E402
import model_store  # noqa: E402

LIVE_BYTES = b"live production weights - must survive"
JOB_ID = "c" * 32


class _Evil:
    def __reduce__(self):  # noqa: ANN204 - pickle protocol hook
        return (os.system, ("echo pwned",))


def checkpoint_bytes(payload: object | None = None) -> bytes:
    """A PyTorch zip-format checkpoint whose pickle uses only plain containers."""
    buf = io.BytesIO()
    stamp = (2026, 1, 1, 0, 0, 0)  # fixed, so the same payload gives the same bytes
    with zipfile.ZipFile(buf, "w") as archive:
        data = payload if payload is not None else {"model": OrderedDict(w=[0.5]), "epoch": -1}
        archive.writestr(zipfile.ZipInfo("best/data.pkl", stamp), pickle.dumps(data, protocol=2))
        archive.writestr(zipfile.ZipInfo("best/version", stamp), "3\n")
    return buf.getvalue()


class TrainContext:
    """Stand-in for JobContext (no Redis / detector controller)."""

    def __init__(self, workspace: Path, params: dict | None = None) -> None:
        self.job_id = JOB_ID
        self.job_type = "train"
        self.params = params or {"output_name": "trained.pt", "force": True}
        self.log_path = workspace / "logs" / f"{JOB_ID}.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.logs: list[str] = []
        self.released = 0

    def training_config(self) -> dict:
        return {
            "defaults": {"workers": 2, "epochs": 1},
            "resources": {"min_mem_available_mb": 1, "min_swap_free_mb": 0},
            "sources": {"roboflow": {"api_key": "redact-me-please"}},
        }

    def publish_progress(self, *args: object, **kwargs: object) -> None:
        pass

    def append_log(self, line: str) -> None:
        self.logs.append(str(line))

    def sanitize(self, text: str) -> str:
        return str(text)

    def gpu_lease_holder(self) -> str:
        return JOB_ID

    def persist_execution(self, value: dict) -> None:
        json.dumps(value)

    def is_cancelled(self) -> bool:
        return False

    def gpu_lease_lost(self) -> bool:
        return False

    def acquire_detector(self) -> dict:
        return {"stopped_by_controller": True}

    def release_detector(self) -> dict:
        self.released += 1
        return {"restored": True}


def _snapshot(_ctx: object) -> dict:
    return {
        "timestamp": "now",
        "mem_available_bytes": 8 * 1024**3,
        "swap_free_bytes": 2 * 1024**3,
        "swap_total_bytes": 2 * 1024**3,
        "cgroup_current_bytes": 1,
        "cgroup_peak_bytes": 1,
        "cgroup_events": {},
        "gpu_lease_holder": JOB_ID,
    }


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    workspace = tmp_path / "workspace"
    data_yaml = workspace / "merged_dataset" / "data.yaml"
    data_yaml.parent.mkdir(parents=True)
    data_yaml.write_text("names: [heron]\n")
    models = tmp_path / "models"
    models.mkdir()
    (models / "yolov8n.pt").write_bytes(b"base model")
    (models / "trained.pt").write_bytes(LIVE_BYTES)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    store = tmp_path / "store"
    monkeypatch.setenv("MODEL_STORE_DIR", str(store))
    monkeypatch.setenv("MODELS_DIR", str(models))
    monkeypatch.setattr(job_runner, "WORKSPACE_DIR", workspace)
    monkeypatch.setattr(job_runner, "MODELS_DIR", str(models))
    monkeypatch.setattr(job_runner, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(job_runner, "_resource_snapshot", _snapshot)
    monkeypatch.setattr(job_runner, "_newest_checkpoint_since", lambda _started: None)
    return {"workspace": workspace, "models": models, "scripts": scripts, "store": store}


def _fake_train(env: dict, body: str, payload: bytes = b"") -> None:
    """Install a CPU stand-in for train.py that runs as the real subprocess."""
    (env["scripts"] / "payload.bin").write_bytes(payload)
    (env["scripts"] / "train.py").write_text(
        textwrap.dedent(
            f"""
            import os, signal, sys
            from pathlib import Path
            out = Path(sys.argv[sys.argv.index("--output") + 1])
            payload = Path({str(env["scripts"] / "payload.bin")!r}).read_bytes()
            print("Epoch 1/1 fake training on CPU", flush=True)
            """
        )
        + textwrap.dedent(body)
    )


def _run(env: dict, monkeypatch: pytest.MonkeyPatch, ctx: TrainContext | None = None) -> dict:
    # The command starts with "python3"; point it at this interpreter.
    original_popen = job_runner.subprocess.Popen

    def popen(cmd: list[str], *args: object, **kwargs: object):  # noqa: ANN202
        if cmd and cmd[0] == "python3":
            cmd = [sys.executable, *cmd[1:]]
        return original_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(job_runner.subprocess, "Popen", popen)
    ctx = ctx or TrainContext(env["workspace"])
    result = job_runner._run_train(ctx)
    assert ctx.released == 1, "detector lease must always be released"
    return result


def _live_untouched(env: dict) -> None:
    assert (env["models"] / "trained.pt").read_bytes() == LIVE_BYTES
    assert sorted(p.name for p in env["models"].iterdir()) == ["trained.pt", "yolov8n.pt"]


def _candidates(env: dict) -> list[dict]:
    return model_store.ModelStore(env["store"], env["models"]).list_candidates()


# ── training publishes candidates ────────────────────────────────────────────


def test_completed_training_creates_candidate_not_live_model(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_train(env, "out.write_bytes(payload)\n", checkpoint_bytes())
    result = _run(env, monkeypatch)

    _live_untouched(env)
    assert "error" not in result, result
    (manifest,) = _candidates(env)
    assert result["candidate_id"] == manifest["id"]
    model = env["store"] / "candidates" / manifest["id"] / manifest["file"]
    assert model.read_bytes() == checkpoint_bytes()
    assert manifest["sha256"] == model_store.sha256_file(model)
    assert manifest["source"] == "training"
    assert manifest["requested_name"] == "trained.pt"
    assert manifest["validation"]["ok"] is True
    prov = manifest["provenance"]
    assert prov["job_id"] == JOB_ID
    assert prov["dataset_yaml_sha256"] == model_store.sha256_file(
        env["workspace"] / "merged_dataset" / "data.yaml"
    )
    assert prov["base_model_sha256"] == model_store.sha256_file(env["models"] / "yolov8n.pt")
    assert len(prov["training_config_sha256"]) == 64
    assert "redact-me-please" not in json.dumps(manifest)
    # The job-unique staging directory is cleaned up after publication.
    assert not (env["workspace"] / "candidates" / JOB_ID).exists()


def test_killed_training_leaves_live_model_and_no_candidate(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = """
    with open(out, "wb") as handle:
        handle.write(payload[: len(payload) // 2])
        handle.flush()
        os.kill(os.getpid(), signal.SIGKILL)
    """
    _fake_train(env, body, checkpoint_bytes())
    result = _run(env, monkeypatch)

    _live_untouched(env)
    assert result["error"] == "train terminated by SIGKILL"
    assert _candidates(env) == []
    assert not (env["workspace"] / "candidates" / JOB_ID).exists()


def test_failed_training_exit_creates_no_candidate(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_train(env, "out.write_bytes(payload)\nsys.exit(3)\n", checkpoint_bytes())
    result = _run(env, monkeypatch)
    _live_untouched(env)
    assert "error" in result
    assert _candidates(env) == []


def test_malicious_training_output_is_rejected(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_train(env, "out.write_bytes(payload)\n", checkpoint_bytes({"x": _Evil()}))
    result = _run(env, monkeypatch)
    _live_untouched(env)
    assert "disallowed code" in result["error"]
    assert _candidates(env) == []
    assert not list((env["store"] / ".staging").iterdir())


def test_crash_during_publication_leaves_nothing_half_published(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_train(env, "out.write_bytes(payload)\n", checkpoint_bytes())

    def crash(*_args: object) -> None:
        raise OSError("simulated power loss before atomic rename")

    monkeypatch.setattr(model_store.os, "rename", crash)
    result = _run(env, monkeypatch)
    _live_untouched(env)
    assert "simulated power loss" in result["error"]
    assert _candidates(env) == []
    assert not list((env["store"] / ".staging").iterdir())


def test_duplicate_output_names_create_distinct_candidates(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_train(env, "out.write_bytes(payload)\n", checkpoint_bytes())
    first = _run(env, monkeypatch)
    _fake_train(env, "out.write_bytes(payload)\n", checkpoint_bytes({"model": {"v": 2}}))
    second = _run(env, monkeypatch)
    _live_untouched(env)
    assert first["candidate_id"] != second["candidate_id"]
    assert {c["requested_name"] for c in _candidates(env)} == {"trained.pt"}
    assert len(_candidates(env)) == 2


@pytest.mark.parametrize("name", [".hidden.pt", "trained.engine", "a b.pt", "x" * 200 + ".pt"])
def test_unsafe_output_name_rejected_before_training(
    env: dict, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _fake_train(env, "out.write_bytes(payload)\n", checkpoint_bytes())
    ctx = TrainContext(env["workspace"], {"output_name": name, "force": True})
    ctx.released = 1  # rejected before the detector lease is taken
    result = _run(env, monkeypatch, ctx)
    _live_untouched(env)
    assert "error" in result
    assert _candidates(env) == []


def test_path_in_output_name_is_confined_to_candidate(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_train(env, "out.write_bytes(payload)\n", checkpoint_bytes())
    result = _run(env, monkeypatch, TrainContext(env["workspace"], {"output_name": "../../x.pt"}))
    _live_untouched(env)
    assert not (env["workspace"].parent / "x.pt").exists()
    (manifest,) = _candidates(env)
    assert manifest["id"] == result["candidate_id"]
    assert manifest["requested_name"] == "x.pt"


# ── promotion and rollback ───────────────────────────────────────────────────


def _store_with_candidate(env: dict, payload: bytes | None = None) -> tuple:
    store = model_store.ModelStore(env["store"], env["models"])
    src = env["workspace"] / "weights.pt"
    src.write_bytes(payload or checkpoint_bytes())
    manifest = store.publish_file(
        src, source="training", requested_name="trained.pt", provenance={"job_id": JOB_ID}
    )
    return store, manifest


def test_promotion_keeps_rollback_copy_and_audit_trail(env: dict) -> None:
    store, manifest = _store_with_candidate(env)
    live = env["models"] / "trained.pt"
    assert store.live_provenance("trained.pt", model_store.sha256_file(live)) == {
        "status": "unresolved"
    }

    record = store.promote(manifest["id"], "trained.pt", actor="pond-admin")

    assert live.read_bytes() == checkpoint_bytes()
    (rollback,) = store.list_rollbacks()
    assert rollback["id"] == record["rollback_id"]
    assert rollback["provenance"] == {"status": "unresolved"}  # legacy stays unresolved
    rollback_file = env["store"] / "rollback" / rollback["id"] / rollback["file"]
    assert rollback_file.read_bytes() == LIVE_BYTES
    assert [h["action"] for h in store.history()] == ["promote", "promote_started"]
    entry = store.history()[0]
    assert entry["actor"] == "pond-admin"
    assert entry["candidate_id"] == manifest["id"]
    assert entry["result_sha256"] == manifest["sha256"]
    assert entry["previous_sha256"] == model_store.sha256_file(rollback_file)
    prov = store.live_provenance("trained.pt", model_store.sha256_file(live))
    assert prov["status"] == "recorded" and prov["candidate_id"] == manifest["id"]
    assert not [p for p in env["models"].iterdir() if p.name.endswith(".tmp")]

    restored = store.rollback(rollback["id"], actor="pond-admin")
    assert live.read_bytes() == LIVE_BYTES
    assert restored["action"] == "rollback"
    # Rolling back snapshots the promoted file too, so it is reversible.
    assert any(r["sha256"] == manifest["sha256"] for r in store.list_rollbacks())


def test_failed_promotion_preserves_live_model(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, manifest = _store_with_candidate(env)

    def broken_replace(*_args: object) -> None:
        raise OSError("disk full during promotion")

    monkeypatch.setattr(model_store.os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        store.promote(manifest["id"], "trained.pt", actor="pond-admin")
    _live_untouched(env)
    assert store.list_rollbacks() == []
    assert [h["action"] for h in store.history()] == ["promote_failed", "promote_started"]
    # The aborted write-ahead record does not give the live file false provenance.
    live_sha = model_store.sha256_file(env["models"] / "trained.pt")
    assert store.live_provenance("trained.pt", live_sha) == {"status": "unresolved"}


def test_failed_ledger_write_undoes_promotion(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, manifest = _store_with_candidate(env)

    real_append = store._append_history

    def no_final_record(record: dict) -> None:
        if record["action"] == "promote":
            raise OSError("history not writable")
        real_append(record)

    monkeypatch.setattr(store, "_append_history", no_final_record)
    with pytest.raises(model_store.ModelStoreError, match="change undone"):
        store.promote(manifest["id"], "trained.pt", actor="pond-admin")
    _live_untouched(env)


def test_tampered_candidate_is_not_promoted(env: dict) -> None:
    store, manifest = _store_with_candidate(env)
    (env["store"] / "candidates" / manifest["id"] / manifest["file"]).write_bytes(
        checkpoint_bytes({"tampered": True})
    )
    with pytest.raises(model_store.ModelStoreError, match="SHA256"):
        store.promote(manifest["id"], "trained.pt", actor="pond-admin")
    _live_untouched(env)


@pytest.mark.parametrize("target", ["../trained.pt", "/models/trained.pt", "x.onnx", ".x.pt"])
def test_promotion_target_must_be_safe_name(env: dict, target: str) -> None:
    store, manifest = _store_with_candidate(env)
    with pytest.raises(model_store.ModelStoreError):
        store.promote(manifest["id"], target, actor="pond-admin")
    _live_untouched(env)


def test_symlinked_live_target_is_refused(env: dict, tmp_path: Path) -> None:
    store, manifest = _store_with_candidate(env)
    outside = tmp_path / "outside.pt"
    outside.write_bytes(b"outside")
    (env["models"] / "linked.pt").symlink_to(outside)
    with pytest.raises(model_store.ModelStoreError, match="regular file"):
        store.promote(manifest["id"], "linked.pt", actor="pond-admin")
    assert outside.read_bytes() == b"outside"


def test_fsync_failure_after_replace_keeps_rollback_copy(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, manifest = _store_with_candidate(env)
    real_fsync_dir = model_store._fsync_dir

    def flaky(path: Path) -> None:
        if Path(path) == env["models"]:
            raise OSError("EIO on directory fsync")
        real_fsync_dir(path)

    monkeypatch.setattr(model_store, "_fsync_dir", flaky)
    record = store.promote(manifest["id"], "trained.pt", actor="pond-admin")
    (rollback,) = store.list_rollbacks()
    assert rollback["id"] == record["rollback_id"]
    rollback_file = env["store"] / "rollback" / rollback["id"] / rollback["file"]
    assert rollback_file.read_bytes() == LIVE_BYTES


def test_stale_promotion_temp_files_are_swept(env: dict) -> None:
    store, manifest = _store_with_candidate(env)
    leftover = env["models"] / ".trained.pt.deadbeef.promote.tmp"
    leftover.write_bytes(b"half copied")
    store.promote(manifest["id"], "pond.pt", actor="pond-admin")
    assert not leftover.exists()


def test_torn_history_line_does_not_swallow_next_record(env: dict) -> None:
    store, manifest = _store_with_candidate(env)
    (env["store"] / "history.jsonl").write_text('{"action": "promote", "target_na')
    store.promote(manifest["id"], "pond.pt", actor="pond-admin")
    assert store.history()[0]["action"] == "promote"


# ── validation never unpickles ───────────────────────────────────────────────


def test_pickle_prefixed_before_zip_is_rejected(tmp_path: Path) -> None:
    # torch.load unpickles a file that does not start with a zip header; the
    # zip reader would still find the archive at the end.
    path = tmp_path / "model.pt"
    path.write_bytes(b"cposix\nsystem\n(S'echo pwned'\ntR." + checkpoint_bytes())
    with pytest.raises(model_store.CandidateValidationError, match="zip-format"):
        model_store.validate_model_file(path, ".pt")


def test_layer_classes_cannot_be_called_with_arguments(tmp_path: Path) -> None:
    # REDUCE ultralytics.nn.tasks.DetectionModel("/elsewhere/evil.pt"): a
    # wildcard-admitted class used as a callable rather than via NEWOBJ.
    smuggled = (
        b"\x80\x02cultralytics.nn.tasks\nDetectionModel\nq\x00"  # GLOBAL, BINPUT
        b"X\x0f\x00\x00\x00/elsewhere/e.ptq\x01"  # BINUNICODE, BINPUT
        b"\x85q\x02Rq\x03."  # TUPLE1, BINPUT, REDUCE, BINPUT, STOP
    )
    path = tmp_path / "model.pt"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("x/data.pkl", smuggled)
    with pytest.raises(model_store.CandidateValidationError, match="REDUCE calls"):
        model_store.validate_model_file(path, ".pt")


@pytest.mark.parametrize(
    "payload,reason",
    [
        (b"not a zip archive", "zip-format"),
        (pickle.dumps({"model": {}}), "zip-format"),
        (b"", "empty"),
    ],
)
def test_structurally_invalid_pt_rejected(tmp_path: Path, payload: bytes, reason: str) -> None:
    path = tmp_path / "model.pt"
    path.write_bytes(payload)
    with pytest.raises(model_store.CandidateValidationError, match=reason):
        model_store.validate_model_file(path, ".pt")


def test_stack_global_and_dotted_names_cannot_smuggle_code(tmp_path: Path) -> None:
    # Protocol 4 uses STACK_GLOBAL; a dotted name walks attributes from an
    # allowed module to arbitrary objects.
    def short_unicode(text: str) -> bytes:
        raw = text.encode()
        return b"\x8c" + bytes([len(raw)]) + raw + b"\x94"  # SHORT_BINUNICODE + MEMOIZE

    smuggled = (
        b"\x80\x04" + short_unicode("ultralytics.nn.tasks") + short_unicode("torch.load")
        + b"\x93."  # STACK_GLOBAL, STOP
    )
    path = tmp_path / "model.pt"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("x/data.pkl", smuggled)
    with pytest.raises(model_store.CandidateValidationError, match="disallowed"):
        model_store.validate_model_file(path, ".pt")
    assert ("ultralytics.nn.tasks", "torch.load") in model_store.scan_pickle_globals(smuggled)


def _g(module: str, name: str) -> bytes:
    return b"c" + module.encode() + b"\n" + name.encode() + b"\n"  # GLOBAL


def _u(text: str) -> bytes:
    raw = text.encode()
    return b"X" + len(raw).to_bytes(4, "little") + raw  # BINUNICODE


_DM = _g("ultralytics.nn.tasks", "DetectionModel")
_REBUILD = _g("torch._tensor", "_rebuild_from_type_v2")
_TENSOR = _g("torch", "Tensor")
# Hand-assembled protocol-2 pickles. Each would call DetectionModel(cfg) at
# load time, and Ultralytics' parse_model eval()s cfg["activation"].
_LAYER_CALL_BYPASSES = {
    "rebuild_from_type_calls_layer": b"\x80\x02" + _REBUILD + b"(" + _DM + _TENSOR
    + b"}" + _u("activation") + _u("x") + b"s\x85}tR.",
    "rebuild_from_type_calls_reconstructor": b"\x80\x02" + _REBUILD + b"("
    + _g("copyreg", "_reconstructor") + _TENSOR + b"(" + _DM + _DM + b"}t}tR.",
    "rebuild_from_type_calls_getattr": b"\x80\x02" + _REBUILD + b"("
    + _g("__builtin__", "getattr") + _TENSOR + b"(" + _DM + _u("x") + b"t}tR.",
    "build_sets_init_on_class": b"\x80\x02" + _g("argparse", "Namespace")
    + b"N}" + _u("__init__") + _DM + b"s\x86b.",
    "reconstructor_with_layer_base": b"\x80\x02" + _g("copyreg", "_reconstructor")
    + b"(" + _DM + _DM + b"}tR.",
    # ns.append = pickle.loads (a module re-export reached through the layer
    # wildcard), then APPEND calls it with attacker bytes.
    "append_calls_reexported_loader": b"\x80\x02" + _g("types", "SimpleNamespace") + b")R}"
    + _u("append") + _g("__builtin__", "getattr") + _g("ultralytics.nn.tasks", "pickle")
    + _u("loads") + b"\x86Rsb" + b"C\x01xa.",
    "append_calls_layer_method": b"\x80\x02" + _g("types", "SimpleNamespace") + b")R}"
    + _u("append") + _g("__builtin__", "getattr") + _DM + _u("load") + b"\x86Rsb"
    + b"C\x01xa.",
    # An instance-level __setstate__ is called by the next BUILD.
    "instance_setstate": b"\x80\x02" + _g("types", "SimpleNamespace") + b")R}"
    + _u("__setstate__") + _g("__builtin__", "getattr") + _DM + _u("load") + b"\x86Rsb"
    + b"C\x01xb.",
    # numpy.ndarray((1,), dtype("O"), b"AAAAAAAA"): an object array over raw
    # bytes, i.e. a forged PyObject pointer.
    "ndarray_forges_object_pointer": b"\x80\x02" + _g("numpy", "ndarray") + b"(K\x01\x85"
    + _g("numpy", "dtype") + _u("O") + b"\x89\x88\x87R"
    + _g("_codecs", "encode") + _u("AAAAAAAA") + _u("latin1") + b"\x86RtR.",
    "object_dtype_scalar": b"\x80\x02" + _g("numpy.core.multiarray", "scalar")
    + _g("numpy", "dtype") + _u("O") + b"\x89\x88\x87R"
    + _g("_codecs", "encode") + _u("AAAAAAAA") + _u("latin1") + b"\x86R\x86R.",
    # model.float = DEFAULT_CFG_DICT.clear: a singleton under a layer module,
    # whose method ultralytics' loader then calls.
    "getattr_on_singleton": b"\x80\x02}" + _u("model") + _DM + b")\x81}" + _u("float")
    + _g("__builtin__", "getattr") + _g("ultralytics.nn.tasks", "DEFAULT_CFG_DICT")
    + _u("clear") + b"\x86Rsbs.",
    # A layer method hidden in a nested hook dict, called during inference.
    "layer_method_as_forward_hook": b"\x80\x02" + _DM + b")\x81}" + _u("_forward_pre_hooks")
    + _g("collections", "OrderedDict") + b")RK\x00" + _g("__builtin__", "getattr")
    + _g("ultralytics.nn.modules.head", "Detect") + _u("forward") + b"\x86Rssb.",
    # NEWOBJ of a module object re-exported under a layer module (torch.nn.functional).
    "newobj_of_module_alias": b"\x80\x02" + _g("ultralytics.nn.modules.block", "F") + b")\x81.",
    # The state dict is changed after BUILD validated it (memo alias).
    "mutate_after_build": b"\x80\x02" + _DM + b")\x81}q\x00b" + b"h\x00" + _u("detect")
    + _g("__builtin__", "getattr") + _g("ultralytics.nn.modules.head", "Detect")
    + _u("forward") + b"\x86Rs0.",
    # A numeric dtype whose BUILD state adds fields (structured, object field).
    "dtype_state_adds_fields": b"\x80\x02" + _g("numpy", "dtype") + _u("f8") + b"\x89\x88\x87R"
    + b"(K\x03" + _u("<") + b"N" + b"(" + _u("a") + b"t}NJ\xff\xff\xff\xffJ\xff\xff\xff\xffK\x00tb.",
}


def _pt_with_pickle(path: Path, data: bytes) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("archive/data.pkl", data)
        archive.writestr("archive/version", "3\n")
    return path


@pytest.mark.parametrize("label", sorted(_LAYER_CALL_BYPASSES))
def test_layer_classes_and_methods_cannot_reach_a_call(tmp_path: Path, label: str) -> None:
    path = _pt_with_pickle(tmp_path / "model.pt", _LAYER_CALL_BYPASSES[label])
    with pytest.raises(model_store.CandidateValidationError, match="disallowed"):
        model_store.validate_model_file(path, ".pt")


def _tensor_pickle(numel: int, view: int) -> bytes:
    """_rebuild_tensor_v2 over storage "0" claiming *numel* floats, viewing *view*."""

    def int2(value: int) -> bytes:
        return b"M" + value.to_bytes(2, "little")  # BININT2

    return (
        b"\x80\x02" + _g("torch._utils", "_rebuild_tensor_v2") + b"(("
        + _u("storage") + _g("torch", "FloatStorage") + _u("0") + _u("cpu") + int2(numel)
        + b"tQK\x00" + int2(view) + b"\x85K\x01\x85\x89"
        + _g("collections", "OrderedDict") + b")RtR."
    )


@pytest.mark.parametrize(
    "numel,view,reason",
    [(4096, 4, "size does not match"), (4, 4096, "exceeds its storage")],
)
def test_tensor_storage_must_match_its_record(
    tmp_path: Path, numel: int, view: int, reason: str
) -> None:
    # torch sizes a storage from the pickle's numel, not from the zip record:
    # a short record lets the tensor read and write past the buffer.
    path = tmp_path / "model.pt"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("archive/data.pkl", _tensor_pickle(numel, view))
        archive.writestr("archive/data/0", b"\x00" * 16)  # four float32 values
    with pytest.raises(model_store.CandidateValidationError, match=reason):
        model_store.validate_model_file(path, ".pt")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("archive/data.pkl", _tensor_pickle(4, 4))
        archive.writestr("archive/data/0", b"\x00" * 16)
    assert model_store.validate_model_file(path, ".pt")["ok"] is True


def _model_with_yaml(model_yaml: dict) -> bytes:
    """DetectionModel NEWOBJ + BUILD {"yaml": model_yaml}, as torch.save writes it."""
    fragment = pickle.dumps(model_yaml, protocol=2)[2:-1]  # drop PROTO and STOP
    return b"\x80\x02" + _DM + b")\x81}" + _u("yaml") + fragment + b"sb."


_BENIGN_YAML = {
    "nc": 3,
    "backbone": [[-1, 1, "Conv", [16, 3, 2]], [-1, 1, "C2f", [32, True]]],
    "head": [[-1, 1, "nn.Upsample", [None, 2, "nearest"]], [[-1, 0], 1, "Detect", ["nc"]]],
}


@pytest.mark.parametrize(
    "model_yaml",
    [
        # Ultralytics' parse_model runs eval(yaml["activation"]) when a model
        # is trained from - the trainer's base model is a promoted file.
        {**_BENIGN_YAML, "activation": "__import__('os').system('echo pwned') or nn.SiLU()"},
        # ... and resolves module names with globals()[name] / getattr(torch.nn, ...).
        {**_BENIGN_YAML, "backbone": [[-1, 1, "attempt_load_one_weight", ["/x.pt"]]]},
        {**_BENIGN_YAML, "head": [[-1, 1, "nn.functional", []]]},
        {**_BENIGN_YAML, "head": "not a list"},
    ],
)
def test_model_yaml_cannot_reach_eval(tmp_path: Path, model_yaml: dict) -> None:
    path = _pt_with_pickle(tmp_path / "model.pt", _model_with_yaml(model_yaml))
    with pytest.raises(model_store.CandidateValidationError, match="model yaml"):
        model_store.validate_model_file(path, ".pt")


def test_yaml_layer_that_downloads_weights_is_rejected(tmp_path: Path) -> None:
    # TorchVision.__init__ fetches torchvision weights when training builds it.
    model_yaml = {
        **_BENIGN_YAML,
        "backbone": [[-1, 1, "TorchVision", [16, "resnet18", "DEFAULT", True, 2]]],
    }
    path = _pt_with_pickle(tmp_path / "model.pt", _model_with_yaml(model_yaml))
    with pytest.raises(model_store.CandidateValidationError, match="unknown module"):
        model_store.validate_model_file(path, ".pt")


@pytest.mark.parametrize(
    "data",
    [
        # Ultralytics keeps train_args["data"] and hands it to check_file(),
        # which downloads URLs (process_video inference path).
        pickle.dumps({"train_args": {"data": "http://127.0.0.1:8765/evil.yaml"}}, 2),
        pickle.dumps({"train_args": {"data": ["ul://evil/data.yaml"]}}, 2),
        b"\x80\x02" + _DM + b")\x81}" + _u("args")
        + pickle.dumps({"data": "https://example.invalid/a.yaml"}, 2)[2:-1] + b"sb.",
    ],
)
def test_training_arguments_cannot_name_urls(tmp_path: Path, data: bytes) -> None:
    path = _pt_with_pickle(tmp_path / "model.pt", data)
    with pytest.raises(model_store.CandidateValidationError, match="URL"):
        model_store.validate_model_file(path, ".pt")


def test_builtin_alias_only_in_protocol_2(tmp_path: Path) -> None:
    # Protocol 3+ does not map __builtin__ to builtins; Ultralytics then tries
    # to pip install the "missing" module.
    for proto, ok in ((2, True), (3, False)):
        data = bytes([0x80, proto]) + _g("__builtin__", "set") + b")R."
        path = _pt_with_pickle(tmp_path / f"p{proto}.pt", data)
        if ok:
            assert model_store.validate_model_file(path, ".pt")["ok"] is True
        else:
            with pytest.raises(model_store.CandidateValidationError, match="protocol 3"):
                model_store.validate_model_file(path, ".pt")


def test_benign_model_yaml_is_accepted(tmp_path: Path) -> None:
    for model_yaml in (_BENIGN_YAML, {**_BENIGN_YAML, "activation": "nn.ReLU()"}):
        path = _pt_with_pickle(tmp_path / "model.pt", _model_with_yaml(model_yaml))
        assert model_store.validate_model_file(path, ".pt")["ok"] is True


def test_torch_save_shaped_layer_pickle_is_accepted(tmp_path: Path) -> None:
    # What torch.save (protocol 2) emits for modules: NEWOBJ + BUILD, with
    # Segment.detect stored as getattr(Detect, "forward"), and a v10Detect head.
    head = "ultralytics.nn.modules.head"
    data = (
        b"\x80\x02}" + _u("model") + _DM + b")\x81}"
        + _u("detect") + _g("__builtin__", "getattr") + _g(head, "Detect") + _u("forward")
        + b"\x86Rs" + _u("_modules") + _g("collections", "OrderedDict") + b")R"
        + _u("head") + _g(head, "v10Detect") + b")\x81}" + _u("nc") + b"K\x03"
        + b"s"  # SETITEM nc
        + b"b"  # BUILD v10Detect
        + b"s"  # SETITEM _modules["head"]
        + b"s"  # SETITEM state["_modules"]
        + b"b"  # BUILD DetectionModel
        + b"s."  # SETITEM outer["model"], STOP
    )
    path = _pt_with_pickle(tmp_path / "model.pt", data)
    assert model_store.validate_model_file(path, ".pt")["ok"] is True


def test_failed_promotion_attempt_does_not_grant_provenance(env: dict) -> None:
    store, manifest = _store_with_candidate(env)
    (env["store"] / "history.jsonl").write_text(
        json.dumps({"action": "promote_started", "at": "t1", "target_name": "pond.pt",
                    "result_sha256": manifest["sha256"], "candidate_id": manifest["id"]})
        + "\n"
        + json.dumps({"action": "promote_failed", "at": "t1", "target_name": "pond.pt",
                      "result_sha256": manifest["sha256"]})
        + "\n"
    )
    assert store.live_provenance("pond.pt", manifest["sha256"]) == {"status": "unresolved"}


def test_real_ultralytics_checkpoint_passes_validation(tmp_path: Path) -> None:
    """Where torch + ultralytics exist (trainer image), a best.pt-shaped file validates.

    Skipped on CPU workers without torch; the allowlist was not exercised
    against a real checkpoint there.
    """
    torch = pytest.importorskip("torch")
    np = pytest.importorskip("numpy")
    tasks = pytest.importorskip("ultralytics.nn.tasks")
    from ultralytics.cfg import get_cfg
    from ultralytics.utils.torch_utils import strip_optimizer

    model = tasks.DetectionModel("yolov8n.yaml", nc=3, verbose=False)
    model.args = get_cfg()
    model.names = {0: "heron", 1: "duck", 2: "raccoon"}
    path = tmp_path / "best.pt"
    torch.save(
        {
            "date": "2026-10-10T00:00:00",
            "epoch": 0,
            "best_fitness": np.float64(0.5),
            "model": model.half(),
            "ema": None,
            "updates": None,
            "optimizer": None,
            "train_args": dict(vars(get_cfg())),
            "train_metrics": {"metrics/mAP50(B)": np.float64(0.6)},
        },
        path,
    )
    assert model_store.validate_model_file(path, ".pt")["ok"] is True
    strip_optimizer(str(path))  # the exact rewrite Ultralytics applies to best.pt
    assert model_store.validate_model_file(path, ".pt")["ok"] is True

    # The same real checkpoint with an eval()-able activation is rejected.
    checkpoint = torch.load(path, map_location="cpu", weights_only=False)
    checkpoint["model"].yaml["activation"] = "__import__('os').getpid() and nn.SiLU()"
    torch.save(checkpoint, path)
    with pytest.raises(model_store.CandidateValidationError, match="activation"):
        model_store.validate_model_file(path, ".pt")
