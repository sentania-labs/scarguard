from __future__ import annotations

from typing import Any

import cloud_controller
from activation_lease import RedisActivationLeases, parse_signed_lease
from actuation_models import DeviceConfig
from cloud_controller import TuyaCloudController


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, key: str, value: str, **_: Any) -> Any:
        pending = getattr(self, "_pending", None)
        if pending is not None:
            pending.append((key, value))
            return self
        self.values[key] = value
        return True

    def pipeline(self, **_: Any) -> FakeRedis:
        self._pending: list[tuple[str, str]] = []
        return self

    def execute(self) -> list[bool]:
        pending = self._pending
        del self._pending
        return [bool(self.set(key, value)) for key, value in pending]

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)

    def eval(
        self,
        script: str,
        count: int,
        key: str,
        deadline: str,
        expected: str,
    ) -> int:
        if self.values.get(key) != expected:
            return 0
        self.delete(deadline)
        return self.delete(key)


class FakeCloud:
    def __init__(self, leases: FakeRedis, signing_key: bytes) -> None:
        self.leases = leases
        self.signing_key = signing_key
        self.commands: list[bool] = []

    def sendcommand(self, device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        value = payload["commands"][0]["value"]
        if value:
            raw = self.leases.get(RedisActivationLeases.redis_key(device_id))
            assert parse_signed_lease(raw, self.signing_key) is not None
        self.commands.append(value)
        return {"success": True}


def test_real_controller_records_finite_lease_before_on(monkeypatch: Any) -> None:
    store = FakeRedis()
    signing_key = bytes(range(32))
    fake_cloud = FakeCloud(store, signing_key)
    monkeypatch.setattr(
        cloud_controller.TuyaCloudController,
        "_bounded_factory",
        staticmethod(lambda factory: fake_cloud),
    )
    monkeypatch.setattr(cloud_controller.time, "sleep", lambda _: None)
    controller = TuyaCloudController(
        chr(120),
        chr(121),
        activation_leases=RedisActivationLeases(store, signing_key),
    )
    device = DeviceConfig(
        name="pond",
        device_id="pond-device",
        type="sprinkler",
    )

    result = controller.activate_device(device, 0.5)

    assert result.success
    assert fake_cloud.commands == [True, False]
    assert store.values == {}


def test_real_controller_refuses_on_when_lease_store_fails(monkeypatch: Any) -> None:
    class BrokenRedis(FakeRedis):
        def set(self, key: str, value: str, **kwargs: Any) -> bool:
            raise ConnectionError("simulated Redis crash")

    store = BrokenRedis()
    signing_key = bytes(range(32))
    fake_cloud = FakeCloud(store, signing_key)
    monkeypatch.setattr(
        cloud_controller.TuyaCloudController,
        "_bounded_factory",
        staticmethod(lambda factory: fake_cloud),
    )
    controller = TuyaCloudController(
        chr(120),
        chr(121),
        activation_leases=RedisActivationLeases(store, signing_key),
    )
    device = DeviceConfig(name="pond", device_id="pond-device", type="sprinkler")

    result = controller.activate_device(device, 0.5)

    assert result.cancelled
    assert fake_cloud.commands == []
