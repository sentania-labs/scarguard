"""Unit tests for /models/{filename}/classes — path-safety + RPC happy/error paths."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/tmp/sg-test-models"))


@pytest.fixture()
def sample_models():
    """Seed a couple of fake model files in MODELS_DIR (conftest points it to /tmp)."""
    pt = MODELS_DIR / "yolov8n.pt"
    engine = MODELS_DIR / "heron.engine"
    pt.write_bytes(b"fake")
    engine.write_bytes(b"fake")
    yield {"pt": pt, "engine": engine}
    pt.unlink(missing_ok=True)
    engine.unlink(missing_ok=True)


class TestSafeResolve:
    def test_rejects_traversal(self, client):
        # The route itself returns 404 on traversal; also the resolver
        # rejects any filename containing a slash.
        resp = client.get("/models/..%2Fetc%2Fpasswd/classes")
        assert resp.status_code == 404

    def test_rejects_absolute_path(self, client):
        resp = client.get("/models/%2Fetc%2Fpasswd/classes")
        assert resp.status_code == 404

    def test_rejects_unknown_suffix(self, client, sample_models):
        # Create a .txt file that shouldn't be introspected
        bad = MODELS_DIR / "readme.txt"
        bad.write_bytes(b"no")
        try:
            resp = client.get("/models/readme.txt/classes")
            assert resp.status_code == 404
        finally:
            bad.unlink(missing_ok=True)

    def test_missing_file_returns_404(self, client):
        resp = client.get("/models/does-not-exist.pt/classes")
        assert resp.status_code == 404


class TestClassesEndpoint:
    def test_happy_path(self, client, sample_models):
        with patch(
            "routes.models._fetch_model_classes_via_redis",
            new=AsyncMock(return_value={
                "ok": True,
                "classes": ["person", "bird", "cat"],
                "warning": None,
            }),
        ):
            resp = client.get("/models/yolov8n.pt/classes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["classes"] == ["person", "bird", "cat"]
        assert data["warning"] is None

    def test_engine_no_names_returns_warning(self, client, sample_models):
        with patch(
            "routes.models._fetch_model_classes_via_redis",
            new=AsyncMock(return_value={
                "ok": True,
                "classes": [],
                "warning": "Class names not embedded in this .engine file",
            }),
        ):
            resp = client.get("/models/heron.engine/classes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["classes"] == []
        assert "not embedded" in data["warning"]

    def test_detector_timeout_surfaces_error(self, client, sample_models):
        with patch(
            "routes.models._fetch_model_classes_via_redis",
            new=AsyncMock(return_value={
                "ok": False,
                "error": "Request timed out",
            }),
        ):
            resp = client.get("/models/yolov8n.pt/classes")
        # Endpoint still returns 200 with ok:false so the client JS can render
        # a nicer error than a hard 500.
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_redis_raises_returns_structured_error(self, client, sample_models):
        from routes import models as models_mod
        models_mod._classes_cache.clear()

        async def _boom(*_a, **_kw):
            raise ConnectionError("simulated redis outage")

        with patch("routes.models._fetch_model_classes_via_redis", side_effect=_boom):
            resp = client.get("/models/yolov8n.pt/classes")
        # Route must return JSON with the normal {ok:false,error:...} shape,
        # not a raw 500 — UI depends on the structured error path.
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "detector" in data["error"].lower() or "unable" in data["error"].lower()

    def test_second_call_uses_cache(self, client, sample_models):
        # Clear the module-level cache so the first call populates it
        from routes import models as models_mod
        models_mod._classes_cache.clear()

        fetch = AsyncMock(return_value={
            "ok": True, "classes": ["x", "y"], "warning": None,
        })
        with patch("routes.models._fetch_model_classes_via_redis", new=fetch):
            first = client.get("/models/yolov8n.pt/classes").json()
            second = client.get("/models/yolov8n.pt/classes").json()

        assert first["ok"] is True
        assert second["ok"] is True
        assert second["cached"] is True
        assert fetch.await_count == 1

    def test_cache_invalidates_on_mtime_change(self, client, sample_models):
        from routes import models as models_mod
        models_mod._classes_cache.clear()

        fetch = AsyncMock(return_value={
            "ok": True, "classes": ["x"], "warning": None,
        })
        with patch("routes.models._fetch_model_classes_via_redis", new=fetch):
            client.get("/models/yolov8n.pt/classes")
        # Bump mtime
        os.utime(str(sample_models["pt"]), (1000, 1000))
        fetch2 = AsyncMock(return_value={
            "ok": True, "classes": ["y"], "warning": None,
        })
        with patch("routes.models._fetch_model_classes_via_redis", new=fetch2):
            resp = client.get("/models/yolov8n.pt/classes")
        assert resp.json()["classes"] == ["y"]
        assert fetch2.await_count == 1
