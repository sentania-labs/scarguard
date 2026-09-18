"""Guard-path tests for the admin group test-fire handler.

This endpoint drives real hardware from a button, so the paths that refuse to
fire matter as much as the one that does. Each test asserts both the published
error and that the controller was never touched.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from actuation_models import (
    ActuationConfig,
    ActuationDefaults,
    DeterrentGroup,
    DeviceConfig,
    TuyaCredentials,
)
from atomic_ref import AtomicRef
from cloud_controller import ActivationResult
from request_handler import TEST_FIRE_GROUP_RESULT_PREFIX, RequestHandler


def _device(name: str, *, enabled: bool = True) -> DeviceConfig:
    return DeviceConfig(name=name, device_id=f"id-{name}", type="sprinkler", enabled=enabled)


class FakeRedis:
    """Captures publishes so a test can read the handler's reply."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, json.loads(payload)))

    def reply_for(self, request_id: str) -> dict[str, Any]:
        channel = f"{TEST_FIRE_GROUP_RESULT_PREFIX}{request_id}"
        for ch, body in self.published:
            if ch == channel:
                return body
        raise AssertionError(f"no reply published on {channel}")


class FakeController:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def activate_device(
        self, device: DeviceConfig, duration: float, *, request_id: str, event_type: str,
    ) -> ActivationResult:
        self.calls.append(device.name)
        return ActivationResult(
            on_success=True, off_success=True, error=None,
            on_ack_ms=5.0, off_attempts=1,
        )


def _handler(cfg: ActuationConfig, controller: FakeController | None) -> tuple[RequestHandler, FakeRedis]:
    return (
        RequestHandler({}, AtomicRef(cfg), AtomicRef(controller)),
        FakeRedis(),
    )


def _cfg(**kw: Any) -> ActuationConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "tuya": TuyaCredentials(api_key="k", api_secret="s"),
        "devices": [],
        "groups": [],
        "defaults": ActuationDefaults(
            device_count_range=[1, 1],
            spray_duration_range=[1.0, 1.0],
            inter_device_delay_range=[0.0, 0.0],
            pre_delay_range=[0.0, 0.0],
        ),
    }
    base.update(kw)
    return ActuationConfig(**base)


@pytest.fixture(autouse=True)
def _no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Persistence is covered by the hash-chain tests; stub it out here."""
    import sys
    import types

    stub = types.ModuleType("actuation_db")
    stub.insert_event = lambda event: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "actuation_db", stub)


class TestGuardPaths:
    def test_missing_group_name_publishes_nothing(self) -> None:
        """No reply channel exists for a malformed request, so stay silent."""
        handler, redis = _handler(_cfg(), FakeController())
        handler._handle_test_fire_group(redis, {"request_id": "r1"})
        assert redis.published == []

    def test_missing_request_id_publishes_nothing(self) -> None:
        handler, redis = _handler(_cfg(), FakeController())
        handler._handle_test_fire_group(redis, {"group_name": "g"})
        assert redis.published == []

    def test_no_controller_refuses(self) -> None:
        handler, redis = _handler(_cfg(), None)
        handler._handle_test_fire_group(redis, {"request_id": "r1", "group_name": "g"})
        reply = redis.reply_for("r1")
        assert reply["ok"] is False
        assert "credentials" in reply["error"].lower()

    def test_unknown_group_refuses_and_fires_nothing(self) -> None:
        controller = FakeController()
        handler, redis = _handler(_cfg(), controller)
        handler._handle_test_fire_group(redis, {"request_id": "r1", "group_name": "ghost"})
        reply = redis.reply_for("r1")
        assert reply["ok"] is False
        assert "not found" in reply["error"]
        assert controller.calls == []

    def test_group_with_only_disabled_devices_refuses(self) -> None:
        """A disabled device must not be fired by the admin path either."""
        controller = FakeController()
        cfg = _cfg(
            devices=[_device("v1", enabled=False)],
            groups=[DeterrentGroup(name="g", devices=["v1"])],
        )
        handler, redis = _handler(cfg, controller)
        handler._handle_test_fire_group(redis, {"request_id": "r1", "group_name": "g"})
        reply = redis.reply_for("r1")
        assert reply["ok"] is False
        assert "no enabled devices" in reply["error"]
        assert controller.calls == []

    def test_empty_group_refuses(self) -> None:
        controller = FakeController()
        cfg = _cfg(devices=[_device("v1")], groups=[DeterrentGroup(name="g", devices=[])])
        handler, redis = _handler(cfg, controller)
        handler._handle_test_fire_group(redis, {"request_id": "r1", "group_name": "g"})
        assert redis.reply_for("r1")["ok"] is False
        assert controller.calls == []


class TestHappyPath:
    def test_fires_group_and_reports_per_device_detail(self) -> None:
        controller = FakeController()
        cfg = _cfg(
            devices=[_device("v1"), _device("v2")],
            groups=[DeterrentGroup(name="g", devices=["v1", "v2"], device_count_range=[2, 2])],
        )
        handler, redis = _handler(cfg, controller)
        handler._handle_test_fire_group(redis, {"request_id": "r1", "group_name": "g"})

        reply = redis.reply_for("r1")
        assert reply["ok"] is True
        assert reply["group_name"] == "g"
        assert reply["devices_fired"] == 2
        assert reply["devices_succeeded"] == 2
        assert sorted(d["device_name"] for d in reply["devices"]) == ["v1", "v2"]
        assert sorted(controller.calls) == ["v1", "v2"]

    def test_group_overrides_beat_global_defaults(self) -> None:
        """The point of the feature is showing the group's real plan."""
        controller = FakeController()
        cfg = _cfg(
            devices=[_device("v1"), _device("v2"), _device("v3")],
            groups=[DeterrentGroup(
                name="g",
                devices=["v1", "v2", "v3"],
                device_count_range=[3, 3],
                spray_duration_range=[2.0, 2.0],
            )],
        )
        handler, redis = _handler(cfg, controller)
        handler._handle_test_fire_group(redis, {"request_id": "r1", "group_name": "g"})

        reply = redis.reply_for("r1")
        assert reply["devices_fired"] == 3
        assert {d["duration_sec"] for d in reply["devices"]} == {2.0}
