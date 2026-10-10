"""FDY-0556 regression coverage against the real watchdog entrypoint/artifacts."""

from __future__ import annotations

import json
import multiprocessing
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from activation_lease import RedisActivationLeases
from main import WatchdogDevice, process_expired_leases, startup_off_sweep


class FakeRedis:
    def __init__(self, values: Any | None = None) -> None:
        self.values: Any = {} if values is None else values

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

    def exists(self, key: str) -> int:
        return int(key in self.values)

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

    def scan_iter(self, match: str) -> list[str]:
        prefix = match.removesuffix("*")
        return [key for key in self.values.keys() if key.startswith(prefix)]


class FakeOffController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def force_off(self, device_id: str, dp_code: str) -> bool:
        self.calls.append((device_id, dp_code))
        return True


def test_expired_lease_recovers_after_deterrent_crash() -> None:
    """A lease left behind by a killed deterrent causes the real handler to OFF."""
    client = FakeRedis()
    signing_key = bytes(range(32))
    leases = RedisActivationLeases(client, signing_key)
    lease = leases.arm("pond-device", 0.5)
    # The deterrent process is now considered killed: it never clears the lease.
    controller = FakeOffController()
    devices = {
        "pond-device": WatchdogDevice(
            name="pond",
            device_id="pond-device",
            type="sprinkler",
        ),
    }

    handled, safe = process_expired_leases(
        client,
        devices,
        controller,
        signing_key,
        now=lease.expires_at + 0.01,
    )

    assert handled == 1
    assert safe
    assert controller.calls == [("pond-device", "switch_1")]
    assert client.values == {}


def test_tampered_or_indefinite_lease_never_authorizes_state_change() -> None:
    client = FakeRedis()
    signing_key = bytes(range(32))
    lease = RedisActivationLeases(client, signing_key).arm("pond-device", 0.5)
    key = next(iter(client.values))
    payload = json.loads(client.values[key])
    payload["expires_at"] = lease.expires_at + 86_400
    client.values[key] = json.dumps(payload)
    controller = FakeOffController()
    devices = {
        "pond-device": WatchdogDevice(
            name="pond",
            device_id="pond-device",
            type="sprinkler",
        ),
    }

    assert process_expired_leases(
        client,
        devices,
        controller,
        signing_key,
        now=time.time() + 90_000,
    ) == (0, True)
    assert controller.calls == []


def test_startup_is_conservative_and_compose_is_independent() -> None:
    controller = FakeOffController()
    devices = {
        "one": WatchdogDevice(name="one", device_id="one", type="light"),
        "two": WatchdogDevice(
            name="two",
            device_id="two",
            type="plug",
            enabled=False,
        ),
    }
    assert startup_off_sweep(devices, controller)
    assert controller.calls == [("one", "switch_led"), ("two", "switch_1")]

    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    service = compose["services"]["off-watchdog"]
    assert "deterrent" not in service.get("depends_on", {})
    assert service["restart"] == "unless-stopped"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["mem_limit"] == "128m"
    assert service["pids_limit"] == 50


def test_off_only_controller_artifact_contains_no_true_switch_command() -> None:
    source = Path("services/off-watchdog/src/off_controller.py").read_text()
    assert '"value": True' not in source
    assert "def activate" not in source
    assert '"value": False' in source


def test_real_entrypoints_recover_after_killed_deterrent(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """Kill a real controller after ON, then run watchdog main to recover."""
    deterrent_src = str(Path("services/deterrent/src").resolve())
    if deterrent_src not in sys.path:
        sys.path.insert(0, deterrent_src)
    import cloud_controller
    from actuation_models import DeviceConfig
    from cloud_controller import TuyaCloudController

    watchdog_main = sys.modules["main"]

    ctx = multiprocessing.get_context("fork")
    with ctx.Manager() as manager:
        values = manager.dict()
        commands = manager.list()
        on_sent = manager.Event()
        signing_key = bytes(range(32))
        store = FakeRedis(values)

        class FakeCloud:
            def sendcommand(
                self,
                device_id: str,
                payload: dict[str, Any],
            ) -> dict[str, Any]:
                value = payload["commands"][0]["value"]
                commands.append(value)
                if value:
                    on_sent.set()
                return {"success": True}

        fake_cloud = FakeCloud()
        monkeypatch.setattr(
            cloud_controller.TuyaCloudController,
            "_bounded_factory",
            staticmethod(lambda factory: fake_cloud),
        )

        def activate() -> None:
            controller = TuyaCloudController(
                chr(120),
                chr(121),
                activation_leases=RedisActivationLeases(store, signing_key),
            )
            controller.activate_device(
                DeviceConfig(
                    name="pond",
                    device_id="pond-device",
                    type="sprinkler",
                ),
                0.5,
            )

        deterrent_process = ctx.Process(target=activate)
        deterrent_process.start()
        assert on_sent.wait(3), "real deterrent controller never sent ON"
        deterrent_process.terminate()
        deterrent_process.join(3)
        assert not deterrent_process.is_alive()
        assert list(commands) == [True]

        off_calls = manager.list()

        class SharedOffController(FakeOffController):
            def force_off(self, device_id: str, dp_code: str) -> bool:
                off_calls.append((device_id, dp_code))
                return True

        config_path = tmp_path / "scarguard.yml"
        config_path.write_text("system: {}\n")
        health_path = tmp_path / "healthy"
        monkeypatch.setattr(watchdog_main, "CONFIG_PATH", str(config_path))
        monkeypatch.setattr(watchdog_main, "HEALTH_PATH", health_path)
        monkeypatch.setattr(watchdog_main, "POLL_INTERVAL_SEC", 0.01)
        monkeypatch.setattr(
            watchdog_main,
            "load_key_from_env",
            lambda _name: signing_key,
        )
        monkeypatch.setattr(
            watchdog_main,
            "load_runtime",
            lambda: (
                {
                    "pond-device": WatchdogDevice(
                        name="pond",
                        device_id="pond-device",
                        type="sprinkler",
                    ),
                },
                SharedOffController(),
                {},
            ),
        )
        monkeypatch.setattr(watchdog_main.redis, "Redis", lambda **_: store)

        watchdog_process = ctx.Process(target=watchdog_main.main)
        watchdog_process.start()
        lease_key = RedisActivationLeases.redis_key("pond-device")
        deadline = time.monotonic() + 6
        while lease_key in values and time.monotonic() < deadline:
            time.sleep(0.02)
        watchdog_process.terminate()
        watchdog_process.join(3)
        if watchdog_process.is_alive():
            watchdog_process.kill()
            watchdog_process.join(3)

        assert lease_key not in values
        assert list(off_calls).count(("pond-device", "switch_1")) >= 2
