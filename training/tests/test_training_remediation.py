"""Regression tests for worker validation, explicit resume, and NVML masking."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import nvml_compat
import pytest
import train
from training_safety import ResumePathError, validate_resume_checkpoint


def test_worker_default_and_orin_bounds() -> None:
    args = train._parse_args(["--data", "/tmp/data.yaml"])
    assert args.workers == 4
    assert train._parse_args(["--data", "/tmp/data.yaml", "--workers", "0"]).workers == 0
    with pytest.raises(SystemExit):
        train._parse_args(["--data", "/tmp/data.yaml", "--workers", "5"])


def test_resume_containment_and_symlink_rejection(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    checkpoint = runs / "train-7" / "weights" / "last.pt"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"checkpoint")
    assert validate_resume_checkpoint(checkpoint, runs) == checkpoint.resolve()

    outside = tmp_path / "outside.pt"
    outside.write_bytes(b"outside")
    with pytest.raises(ResumePathError):
        validate_resume_checkpoint(outside, runs)

    symlink = runs / "train-7" / "weights" / "linked.pt"
    symlink.symlink_to(checkpoint)
    with pytest.raises(ResumePathError):
        validate_resume_checkpoint(symlink, runs)

    linked_run = runs / "linked-run"
    linked_run.symlink_to(checkpoint.parent.parent, target_is_directory=True)
    with pytest.raises(ResumePathError):
        validate_resume_checkpoint(linked_run / "weights" / "last.pt", runs)


def test_resume_calls_ultralytics_without_new_project(tmp_path: Path, monkeypatch) -> None:
    dataset = tmp_path / "dataset" / "data.yaml"
    dataset.parent.mkdir()
    dataset.write_text("names: [heron]\n")
    runs = tmp_path / "runs"
    checkpoint = runs / "train-3" / "weights" / "last.pt"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"last")
    best = checkpoint.parent / "best.pt"
    best.write_bytes(b"best")
    output = tmp_path / "models" / "pond.pt"
    calls: list[dict] = []

    class FakeYOLO:
        def __init__(self, model: str) -> None:
            assert model == str(checkpoint.resolve())
            self.trainer = SimpleNamespace(best=best)

        def train(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(results_dict={})

    monkeypatch.setitem(sys.modules, "ultralytics", SimpleNamespace(YOLO=FakeYOLO))
    monkeypatch.setattr(train, "_validate_dataset", lambda _path: {"names": ["heron"]})
    monkeypatch.setattr(train, "_count_per_class", lambda *_args: {0: 500})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "train.py",
            "--data",
            str(dataset),
            "--project",
            str(runs),
            "--resume-from",
            str(checkpoint),
            "--workers",
            "4",
            "--output",
            str(output),
        ],
    )
    train.main()

    assert calls == [{"resume": str(checkpoint.resolve()), "workers": 4, "verbose": True}]
    assert output.read_bytes() == b"best"


def test_known_nvml_assert_becomes_chained_cuda_oom() -> None:
    class FakeCuda:
        @staticmethod
        def mem_get_info() -> tuple[int, int]:
            return 128 * 1024 * 1024, 8 * 1024 * 1024 * 1024

        @staticmethod
        def memory_allocated() -> int:
            return 5 * 1024 * 1024 * 1024

        @staticmethod
        def memory_reserved() -> int:
            return 6 * 1024 * 1024 * 1024

    class FakeOOM(RuntimeError):
        pass

    fake_torch = SimpleNamespace(cuda=FakeCuda(), OutOfMemoryError=FakeOOM)
    original = RuntimeError(
        "NVML_SUCCESS == r INTERNAL ASSERT FAILED at CUDACachingAllocator.cpp:838"
    )
    translated = nvml_compat.translate_masked_cuda_oom(original, fake_torch)
    assert isinstance(translated, FakeOOM)
    assert "CUDA out of memory" in str(translated)
    assert "128 MiB free" in str(translated)


def test_unrelated_runtime_error_is_not_reclassified() -> None:
    assert nvml_compat.translate_masked_cuda_oom(RuntimeError("bad labels"), object()) is None
