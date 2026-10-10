from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Test exercises real PyTorch loading which requires torch.
# In environments where torch is not installed, it will skip gracefully.
# The CI environment provides torch in the service container.
torch = pytest.importorskip("torch")

import model_classes_handler as mch  # noqa: E402
from model_classes_handler import ModelClassesHandler  # noqa: E402


class SentinelFired(Exception):
    pass

class MaliciousSentinel:
    def __reduce__(self):
        # We raise a custom exception to prove arbitrary code execution occurred.
        # This is safe and robust, avoiding filesystem touches that might fail in sandboxes.
        def _explode():
            raise SentinelFired("Arbitrary code execution proved!")
        return (_explode, ())

def test_regression_arbitrary_code_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mch, "_MODELS_ROOT", tmp_path.resolve())

    # Create a malicious checkpoint
    malicious_path = tmp_path / "malicious.pt"
    # Create the zip format PyTorch expects to trigger the load
    torch.save(MaliciousSentinel(), malicious_path)

    # Create a compatible fixture (just weights, no custom classes)
    compatible_path = tmp_path / "compatible.pt"
    torch.save({"weights": torch.tensor([1.0, 2.0])}, compatible_path)

    handler = ModelClassesHandler(
        redis_cfg={"host": "localhost", "port": 6379},
        stop_event=MagicMock(),
    )

    # Introspect malicious
    try:
        res_malicious = handler._introspect(str(malicious_path))
    except SentinelFired:
        pytest.fail("Vulnerability is present: Arbitrary code execution occurred!")

    # If the handler successfully blocked it, it should return an error
    assert res_malicious["ok"] is False
    err_str = str(res_malicious.get("error", "")).lower()
    assert "conversion" in err_str or "pickle" in err_str or "replace" in err_str, (
        f"Expected clear conversion or replacement message, got: {err_str}"
    )

    # Introspect compatible
    res_compatible = handler._introspect(str(compatible_path))
    # It might fail because it doesn't have names, but it shouldn't fail with the unrestricted pickle error
    err_comp = str(res_compatible.get("error", "")).lower()
    assert "pickle" not in err_comp, "Compatible checkpoint rejected due to pickle"

def test_regression_url_download_and_escapes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mch, "_MODELS_ROOT", tmp_path.resolve())

    handler = ModelClassesHandler(
        redis_cfg={"host": "localhost", "port": 6379},
        stop_event=MagicMock(),
    )

    url_res = handler._introspect("https://github.com/ultralytics/assets/releases/download/v8.2.0/yolov8n.pt")
    assert url_res["ok"] is False
    assert "url" in str(url_res.get("error", "")).lower() or "not found" in str(url_res.get("error", "")).lower()

