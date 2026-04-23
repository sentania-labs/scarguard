"""Tests for the /admin/deterrent test-fire validation and force-off route.

These verify the v1.14 physical-safety contract at the web boundary:
duration_sec must be a finite number in [MIN_ACTUATION_SEC, MAX_TEST_FIRE_SEC]
and out-of-range values get a 400, not a silent clamp."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture()
def fake_redis(monkeypatch):
    """Stub the deterrent Redis request/response helper so the test never
    touches a real Redis. Returns whatever we tell it to."""
    captured: dict[str, Any] = {"payloads": []}

    async def _fake_request(channel: str, prefix: str, payload: dict[str, Any], timeout_sec: float = 15.0) -> dict[str, Any]:
        captured["payloads"].append({"channel": channel, "payload": payload})
        return captured.get("response", {"ok": True, "device_name": "x"})

    monkeypatch.setattr(
        "routes.deterrent._redis_request", _fake_request,
    )
    return captured


class TestTestFireValidation:
    def test_rejects_negative_duration(self, client, fake_redis) -> None:
        resp = client.post(
            "/admin/deterrent/test-fire",
            json={"device_id": "bf123", "duration_sec": -1.0},
        )
        assert resp.status_code == 400
        assert "duration_sec" in resp.json()["error"]
        # Should not have reached the deterrent service.
        assert fake_redis["payloads"] == []

    def test_rejects_oversized_duration(self, client, fake_redis) -> None:
        resp = client.post(
            "/admin/deterrent/test-fire",
            json={"device_id": "bf123", "duration_sec": 86400.0},
        )
        assert resp.status_code == 400
        assert "duration_sec" in resp.json()["error"]
        assert fake_redis["payloads"] == []

    def test_rejects_nan(self, client, fake_redis) -> None:
        # JSON has no NaN; the route must reject if Python receives it.
        # We send the literal string "NaN" which is allowed by Python's
        # json.loads by default — confirms the math.isnan() guard fires.
        resp = client.post(
            "/admin/deterrent/test-fire",
            content='{"device_id": "bf123", "duration_sec": NaN}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_rejects_string_duration(self, client, fake_redis) -> None:
        resp = client.post(
            "/admin/deterrent/test-fire",
            json={"device_id": "bf123", "duration_sec": "forever"},
        )
        assert resp.status_code == 400
        assert fake_redis["payloads"] == []

    def test_accepts_in_range(self, client, fake_redis) -> None:
        resp = client.post(
            "/admin/deterrent/test-fire",
            json={"device_id": "bf123", "duration_sec": 5.0},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert fake_redis["payloads"][0]["payload"]["duration_sec"] == 5.0

    def test_default_when_omitted(self, client, fake_redis) -> None:
        resp = client.post(
            "/admin/deterrent/test-fire",
            json={"device_id": "bf123"},
        )
        assert resp.status_code == 200
        # Default test-fire is 3.0s.
        assert fake_redis["payloads"][0]["payload"]["duration_sec"] == 3.0

    def test_rejects_missing_device_id(self, client, fake_redis) -> None:
        resp = client.post("/admin/deterrent/test-fire", json={"duration_sec": 3.0})
        assert resp.status_code == 400
        assert fake_redis["payloads"] == []

    def test_at_max_boundary_accepted(self, client, fake_redis) -> None:
        from deterrent_safety import MAX_TEST_FIRE_SEC
        resp = client.post(
            "/admin/deterrent/test-fire",
            json={"device_id": "bf123", "duration_sec": MAX_TEST_FIRE_SEC},
        )
        assert resp.status_code == 200

    def test_just_above_max_rejected(self, client, fake_redis) -> None:
        from deterrent_safety import MAX_TEST_FIRE_SEC
        resp = client.post(
            "/admin/deterrent/test-fire",
            json={"device_id": "bf123", "duration_sec": MAX_TEST_FIRE_SEC + 0.001},
        )
        assert resp.status_code == 400


class TestForceOff:
    def test_returns_per_device_result(self, client, fake_redis) -> None:
        fake_redis["response"] = {
            "ok": True,
            "devices": [
                {"device_id": "a", "name": "sprinkler-1", "ok": True, "error": None},
                {"device_id": "b", "name": "sprinkler-2", "ok": True, "error": None},
            ],
        }
        resp = client.post("/admin/deterrent/force-off")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert len(body["devices"]) == 2

    def test_502_on_partial_failure(self, client, fake_redis) -> None:
        fake_redis["response"] = {
            "ok": False,
            "devices": [
                {"device_id": "a", "name": "sprinkler-1", "ok": True, "error": None},
                {"device_id": "b", "name": "sprinkler-2", "ok": False, "error": "stuck"},
            ],
        }
        resp = client.post("/admin/deterrent/force-off")
        assert resp.status_code == 502
        body = resp.json()
        assert body["ok"] is False
