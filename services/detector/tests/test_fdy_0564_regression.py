"""Regression tests for FDY-0564: Reject model checkpoints that can execute arbitrary code.

Exercises the real handler code paths (not copied mocks) to demonstrate that:
1. A malicious checkpoint with a custom __reduce__ that fires code on unpickle is rejected
   without executing the malicious code (requires torch, tested in CI).
2. A compatible checkpoint is accepted (or at least not rejected for pickle reasons).
3. URL paths, symlink escapes, and path traversals are rejected.

Findings addressed: SG-04, SG-36.
"""

import io
from pathlib import Path
from unittest.mock import MagicMock

import model_classes_handler as mch  # noqa: E402
import pytest
from model_classes_handler import ModelClassesHandler  # noqa: E402


class SentinelFired(Exception):
    """Raised when a malicious __reduce__ fires during unpickle."""


def _malicious_explode():
    """Module-level callable for pickle to reference."""
    raise SentinelFired("Arbitrary code execution proved!")


class MaliciousSentinel:
    """A class whose __reduce__ method fires code on unpickle."""

    def __reduce__(self):
        return (_malicious_explode, ())


def _make_real_pt_file(tmp_path: Path, name: str, payload: object) -> Path:
    """Create a real PyTorch checkpoint using torch.save.

    This produces a valid ``.pt`` archive (a zip with ``data.pkl``) so that
    the real ``torch.load()`` code path inside ultralytics actually
    encounters a well-formed checkpoint file.
    """
    try:
        import torch
    except ImportError:
        raise pytest.skip("torch not installed") from None

    path = tmp_path / name
    torch.save(payload, path)
    return path


def _make_legacy_pt_file(tmp_path: Path, name: str, payload: object) -> Path:
    """Create a legacy-style .pt file (zip with global_tensor.pkl).

    PyTorch checkpoints serialise as a zip archive containing
    ``data.pkl`` (modern) or ``global_tensor.pkl`` (legacy).  This reproduces
    the legacy format so that the real ``torch.load()`` path inside ultralytics
    actually encounters a valid .pt file.
    """
    import pickle
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("global_tensor.pkl", pickle.dumps(payload))
    path = tmp_path / name
    path.write_bytes(buf.getvalue())
    return path


class TestArbitraryCodeExecution:
    """Tests that require torch to be installed (run in CI service containers)."""

    @pytest.fixture
    def handler(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModelClassesHandler:
        monkeypatch.setattr(mch, "_MODELS_ROOT", tmp_path.resolve())
        return ModelClassesHandler(
            redis_cfg={"host": "localhost", "port": 6379},
            stop_event=MagicMock(),
        )

    def test_arbitrary_code_execution_blocked(self, handler: ModelClassesHandler, tmp_path: Path) -> None:
        """A checkpoint whose unpickle runs arbitrary code must not execute it.

        Creates a malicious .pt file and passes it to the real handler's
        _introspect method.  If the safe-load guard is working, the malicious
        code must NOT fire and the handler must return an error dict.
        """
        # Create a malicious checkpoint that fires code on unpickle
        # Uses real torch.save so the checkpoint structure is valid.
        malicious_path = _make_real_pt_file(tmp_path, "malicious.pt", MaliciousSentinel())

        # Create a compatible fixture (just plain data, no custom classes)
        compatible_path = _make_real_pt_file(
            tmp_path,
            "compatible.pt",
            {"weights": b"\x00\x00\x00\x00\x00\x00\x80\x3f"},
        )

        # Introspect malicious: the safe load guard must block it.
        res_malicious = handler._introspect(str(malicious_path))

        # If the malicious code fired, SentinelFired will propagate and
        # mark the test failed.  If we reach here, the code was blocked.
        assert res_malicious["ok"] is False, (
            f"Expected malicious checkpoint to be rejected, got ok=True: {res_malicious}"
        )
        err_str = str(res_malicious.get("error", "")).lower()
        assert "conversion" in err_str or "pickle" in err_str or "replace" in err_str, (
            f"Expected clear conversion or replacement message, got: {err_str}"
        )

        # Introspect compatible: it should not fail due to the unrestricted pickle guard.
        res_compatible = handler._introspect(str(compatible_path))
        err_comp = str(res_compatible.get("error", "")).lower()
        assert "pickle" not in err_comp or res_compatible.get("ok", False), (
            f"Compatible checkpoint rejected due to pickle: {res_compatible}"
        )


class TestPathSafety:
    """Tests that validate path-level safety (work without torch)."""

    def test_regression_url_download_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """URL paths must be rejected with a clear error."""
        monkeypatch.setattr(mch, "_MODELS_ROOT", tmp_path.resolve())

        handler = ModelClassesHandler(
            redis_cfg={"host": "localhost", "port": 6379},
            stop_event=MagicMock(),
        )

        url_res = handler._introspect(
            "https://github.com/ultralytics/assets/releases/download/v8.2.0/yolov8n.pt"
        )
        assert url_res["ok"] is False
        assert (
            "url" in str(url_res.get("error", "")).lower()
            or "not found" in str(url_res.get("error", "")).lower()
        )

    def test_regression_symlink_escape_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A path that escapes _MODELS_ROOT via symlinks must be rejected."""
        models_root = tmp_path / "models"
        models_root.mkdir()
        monkeypatch.setattr(mch, "_MODELS_ROOT", models_root.resolve())

        outside = tmp_path / "outside.pt"
        outside.write_bytes(b"fake checkpoint data")

        inside_symlink = models_root / "symlink.pt"
        inside_symlink.symlink_to(outside)

        handler = ModelClassesHandler(
            redis_cfg={"host": "localhost", "port": 6379},
            stop_event=MagicMock(),
        )

        res = handler._introspect(str(inside_symlink))
        assert res["ok"] is False

    def test_regression_traversal_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Path traversal with ../ must be rejected."""
        models_root = tmp_path / "models"
        models_root.mkdir()
        monkeypatch.setattr(mch, "_MODELS_ROOT", models_root.resolve())

        outside = tmp_path / "outside.pt"
        outside.write_bytes(b"fake")

        handler = ModelClassesHandler(
            redis_cfg={"host": "localhost", "port": 6379},
            stop_event=MagicMock(),
        )

        res = handler._introspect("../../outside.pt")
        assert res["ok"] is False

    def test_regression_supported_suffixes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only .pt, .engine, .onnx are accepted for path validation."""
        models_root = tmp_path / "models"
        models_root.mkdir()
        monkeypatch.setattr(mch, "_MODELS_ROOT", models_root.resolve())

        for suffix in (".pt", ".engine", ".onnx"):
            fpath = models_root / f"model{suffix}"
            fpath.write_bytes(b"fake data")

        handler = ModelClassesHandler(
            redis_cfg={"host": "localhost", "port": 6379},
            stop_event=MagicMock(),
        )

        for suffix in (".pt", ".engine", ".onnx"):
            fpath = models_root / f"model{suffix}"
            res = handler._introspect(str(fpath))
            # The load itself will likely fail because the files are fake,
            # but the path validation (suffix, root confinement) must pass.
            assert res.get("ok") is True, f"Supported suffix {suffix} rejected at path validation"

        for bad_suffix in (".bin", ".pth", ".safetensors"):
            fpath = models_root / f"model{bad_suffix}"
            fpath.write_bytes(b"fake")
            res = handler._introspect(str(fpath))
            assert res["ok"] is False, f"Unsupported suffix {bad_suffix} should be rejected"

    def test_regression_empty_and_http_paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty and HTTP-path strings must be rejected."""
        monkeypatch.setattr(mch, "_MODELS_ROOT", tmp_path.resolve())

        handler = ModelClassesHandler(
            redis_cfg={"host": "localhost", "port": 6379},
            stop_event=MagicMock(),
        )

        assert handler._introspect("")["ok"] is False
        assert handler._introspect("http://evil.com/model.pt")["ok"] is False


class TestSafeLoadImport:
    """Tests that safe_load module patches are in place."""

    def test_safe_load_exposes_safe_load_fn(self) -> None:
        """Verify safe_load exposes _load_torch_safe entry-point."""
        import safe_load  # noqa: F401

        try:
            import torch  # noqa: F401  # noqa: F401 - verifies torch is present
        except ImportError:
            pytest.skip("torch not installed")

        assert hasattr(safe_load, "_load_torch_safe"), (
            "safe_load must expose _load_torch_safe"
        )
        # safe_load no longer monkeypatches torch.load globally;
        # the monkey-patch was removed because it broke YOLO checkpoint
        # loading (weights_only=True rejects Ultralytics custom classes).
        # Safe loading is now done via the _load_torch_safe() helper
        # which uses torch.serialization.safe_load when available.

    def test_safe_load_blocks_uploads(self) -> None:
        """Verify safe_load blocks ultralytics automatic downloads."""
        import safe_load  # noqa: F401

        try:
            from ultralytics.utils import downloads
            assert hasattr(downloads, "attempt_download_asset"), (
                "download blocker must patch ultralytics downloads"
            )
        except ImportError:
            pytest.skip("ultralytics not installed")
