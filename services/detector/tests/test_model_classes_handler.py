"""Unit tests for ModelClassesHandler — focus on the pure logic paths.

The Redis subscription loop is integration-tested via manual smoke tests;
these unit tests cover classification of names, cache semantics, the
safe-path gate, and the pool/CPU-fallback preference.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


# The module computes _MODELS_ROOT at import time.  Point it at the test
# tmp dir BEFORE importing so _safe_model_path accepts our fixtures.
_TEST_MODELS_DIR = Path("/tmp/sg-detector-test-models")
_TEST_MODELS_DIR.mkdir(exist_ok=True)
os.environ["MODELS_DIR"] = str(_TEST_MODELS_DIR)

import model_classes_handler as mch  # noqa: E402
from model_classes_handler import (  # noqa: E402
    ModelClassesHandler,
    _extract_classes,
    _looks_like_stub_names,
    _normalize_names,
    _safe_model_path,
)


def _fake_model(names):
    m = MagicMock()
    m.names = names
    return m


@pytest.fixture(autouse=True)
def _reset_models_root(monkeypatch):
    monkeypatch.setattr(mch, "_MODELS_ROOT", _TEST_MODELS_DIR.resolve())
    yield


@pytest.fixture()
def pt_file():
    f = _TEST_MODELS_DIR / "yolov8n.pt"
    f.write_bytes(b"x")
    yield f
    f.unlink(missing_ok=True)


@pytest.fixture()
def engine_file():
    f = _TEST_MODELS_DIR / "heron.engine"
    f.write_bytes(b"x")
    yield f
    f.unlink(missing_ok=True)


class TestStubNameDetection:
    def test_empty_is_stub(self):
        assert _looks_like_stub_names([]) is True

    def test_all_class_N(self):
        assert _looks_like_stub_names(["class_0", "class_1", "class_2"]) is True

    def test_mixed_is_not_stub(self):
        assert _looks_like_stub_names(["class_0", "heron"]) is False

    def test_real_names(self):
        assert _looks_like_stub_names(["person", "bird", "cat"]) is False


class TestNormalizeNames:
    def test_dict_names_preserves_sorted_order(self):
        classes, warning = _normalize_names({2: "cat", 0: "person", 1: "bird"}, ".pt")
        assert classes == ["person", "bird", "cat"]
        assert warning is None

    def test_list_names(self):
        classes, warning = _normalize_names(["a", "b"], ".pt")
        assert classes == ["a", "b"]
        assert warning is None

    def test_engine_with_stub_names_returns_warning(self):
        classes, warning = _normalize_names({0: "class_0", 1: "class_1"}, ".engine")
        assert classes == []
        assert warning is not None and ".engine" in warning

    def test_engine_with_real_names_keeps_them(self):
        classes, warning = _normalize_names({0: "great_blue_heron", 1: "duck"}, ".engine")
        assert classes == ["great_blue_heron", "duck"]
        assert warning is None

    def test_empty_returns_warning(self):
        classes, warning = _normalize_names({}, ".pt")
        assert classes == []
        assert warning is not None


class TestSafePath:
    def test_accepts_file_under_models_root(self, pt_file):
        assert _safe_model_path(str(pt_file)) == pt_file.resolve()

    def test_rejects_outside_root(self, tmp_path):
        outside = tmp_path / "elsewhere.pt"
        outside.write_bytes(b"x")
        assert _safe_model_path(str(outside)) is None

    def test_rejects_unknown_suffix(self):
        odd = _TEST_MODELS_DIR / "notes.txt"
        odd.write_bytes(b"x")
        try:
            assert _safe_model_path(str(odd)) is None
        finally:
            odd.unlink(missing_ok=True)

    def test_rejects_missing(self):
        assert _safe_model_path(str(_TEST_MODELS_DIR / "ghost.pt")) is None

    def test_rejects_empty_or_nonstring(self):
        assert _safe_model_path("") is None
        assert _safe_model_path(None) is None  # type: ignore[arg-type]


class TestIntrospect:
    def _make_handler(self, pool=None):
        return ModelClassesHandler(
            redis_cfg={"host": "redis", "port": 6379},
            stop_event=threading.Event(),
            model_pool=pool,
        )

    def test_missing_file_returns_error(self):
        h = self._make_handler()
        r = h._introspect(str(_TEST_MODELS_DIR / "ghost.pt"))
        assert r["ok"] is False

    def test_path_outside_models_dir_rejected(self, tmp_path):
        outside = tmp_path / "malicious.pt"
        outside.write_bytes(b"x")
        r = self._make_handler()._introspect(str(outside))
        assert r["ok"] is False
        assert "outside" in r["error"] or "not found" in r["error"]

    def test_reuses_pool_without_load(self, pt_file):
        # Mock pool that has the file already loaded.
        pool = MagicMock()
        pool._models = {str(pt_file.resolve()): MagicMock(_model=_fake_model({0: "from_pool"}))}
        h = self._make_handler(pool=pool)
        with patch("model_classes_handler._load_yolo") as YOLO, \
             patch("model_classes_handler._names_from_pt_cpu") as cpu:
            r = h._introspect(str(pt_file))
        assert r["ok"] is True
        assert r["classes"] == ["from_pool"]
        # Neither fallback should have been called.
        assert YOLO.call_count == 0
        assert cpu.call_count == 0

    def test_falls_back_to_cpu_torch_for_pt(self, pt_file):
        h = self._make_handler()  # no pool
        with patch("model_classes_handler._names_from_pt_cpu", return_value={0: "from_cpu"}), \
             patch("model_classes_handler._load_yolo") as YOLO:
            r = h._introspect(str(pt_file))
        assert r["ok"] is True
        assert r["classes"] == ["from_cpu"]
        assert YOLO.call_count == 0  # full load avoided

    def test_falls_back_to_yolo_for_engine(self, engine_file):
        h = self._make_handler()
        with patch(
            "model_classes_handler._load_yolo",
            return_value=_fake_model({0: "heron", 1: "duck"}),
        ):
            r = h._introspect(str(engine_file))
        assert r["ok"] is True
        assert r["classes"] == ["heron", "duck"]

    def test_caches_by_path_mtime_and_size(self, pt_file):
        h = self._make_handler()
        with patch("model_classes_handler._names_from_pt_cpu", return_value={0: "a"}) as cpu:
            first = h._introspect(str(pt_file))
            second = h._introspect(str(pt_file))
        assert first["cached"] is False
        assert second["cached"] is True
        assert second["classes"] == ["a"]
        assert cpu.call_count == 1

    def test_cache_invalidates_on_mtime_change(self, pt_file):
        h = self._make_handler()
        with patch("model_classes_handler._names_from_pt_cpu", return_value={0: "a"}):
            h._introspect(str(pt_file))
        os.utime(str(pt_file), (100, 100))
        with patch("model_classes_handler._names_from_pt_cpu", return_value={0: "b"}):
            r = h._introspect(str(pt_file))
        assert r["cached"] is False
        assert r["classes"] == ["b"]

    def test_cache_invalidates_on_size_change(self, pt_file):
        h = self._make_handler()
        with patch("model_classes_handler._names_from_pt_cpu", return_value={0: "a"}):
            h._introspect(str(pt_file))
        # Rewrite with more bytes but try to preserve mtime — proves size
        # entering the key matters.
        st = pt_file.stat()
        pt_file.write_bytes(b"xxxx")
        os.utime(str(pt_file), (st.st_atime, st.st_mtime))
        with patch("model_classes_handler._names_from_pt_cpu", return_value={0: "b"}):
            r = h._introspect(str(pt_file))
        # mtime probably also bumped by the write; accept either cache bust
        # path — key thing is the stale value isn't returned.
        assert r["classes"] == ["b"]


class TestExtractClassesLegacy:
    """Retain coverage of the test-only helper used by v0.13.4 early tests."""

    def test_happy_path(self, pt_file):
        with patch("model_classes_handler._load_yolo") as YOLO:
            YOLO.return_value = _fake_model({0: "person", 1: "bird"})
            classes, warning = _extract_classes(str(pt_file))
        assert classes == ["person", "bird"]
        assert warning is None
