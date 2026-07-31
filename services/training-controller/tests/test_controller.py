"""Ownership and recovery tests for the detector lifecycle boundary."""

from __future__ import annotations

import json
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import main
import pytest


class FakeBackend:
    def __init__(self, running: bool = True, summary_state: str | None = None) -> None:
        self.running = running
        self.summary_state = summary_state or ("running" if running else "exited")
        self.exists = True
        self.stop_calls: list[str] = []
        self.start_calls: list[str] = []

    def find_detector(self):
        return {"Id": "detector-id", "State": self.summary_state}

    def inspect(self, container_id: str):
        assert container_id == "detector-id"
        return (
            {
                "State": {
                    "Running": self.running,
                    "Paused": self.summary_state == "paused" and self.running,
                    "Restarting": self.summary_state == "restarting" and self.running,
                }
            }
            if self.exists
            else None
        )

    def stop(self, container_id: str):
        self.stop_calls.append(container_id)
        self.running = False
        self.summary_state = "exited"
        return True

    def start(self, container_id: str):
        self.start_calls.append(container_id)
        self.running = True


OWNER = "a" * 32
OTHER = "b" * 32


def test_running_detector_is_stopped_and_restored(tmp_path: Path) -> None:
    backend = FakeBackend(running=True)
    controller = main.DetectorLeaseController(backend, tmp_path / "state.json", lambda: OWNER)
    state = controller.acquire(OWNER)
    assert state["stopped_by_controller"] is True
    assert backend.stop_calls == ["detector-id"]
    released = controller.release(OWNER)
    assert released["restored"] is True
    assert backend.start_calls == ["detector-id"]


def test_pre_stopped_detector_is_never_started(tmp_path: Path) -> None:
    backend = FakeBackend(running=False)
    controller = main.DetectorLeaseController(backend, tmp_path / "state.json", lambda: OWNER)
    state = controller.acquire(OWNER)
    assert state["stopped_by_controller"] is False
    controller.release(OWNER)
    assert backend.stop_calls == []
    assert backend.start_calls == []


def test_operator_stop_race_is_not_claimed_or_reversed(tmp_path: Path) -> None:
    class OperatorWonBackend(FakeBackend):
        def stop(self, container_id: str):
            self.stop_calls.append(container_id)
            self.running = False
            self.summary_state = "exited"
            return False

    backend = OperatorWonBackend(running=True)
    controller = main.DetectorLeaseController(backend, tmp_path / "state.json", lambda: OWNER)
    state = controller.acquire(OWNER)
    assert state["stopped_by_controller"] is False
    controller.release(OWNER)
    assert backend.start_calls == []


def test_foreign_owner_cannot_release_or_replace_lease(tmp_path: Path) -> None:
    backend = FakeBackend()
    controller = main.DetectorLeaseController(backend, tmp_path / "state.json", lambda: OWNER)
    controller.acquire(OWNER)
    with pytest.raises(main.ControllerError):
        controller.acquire(OTHER)
    with pytest.raises(main.ControllerError):
        controller.release(OTHER)
    assert backend.start_calls == []


def test_heartbeat_reasserts_owned_detector_stop(tmp_path: Path) -> None:
    backend = FakeBackend()
    controller = main.DetectorLeaseController(backend, tmp_path / "state.json", lambda: OWNER)
    controller.acquire(OWNER)
    backend.running = True  # daemon/operator restarted the exact owned container
    controller.heartbeat(OWNER)
    assert backend.stop_calls == ["detector-id", "detector-id"]


def test_stale_recovery_restores_only_owned_container(tmp_path: Path, monkeypatch) -> None:
    backend = FakeBackend()
    state_path = tmp_path / "state.json"
    controller = main.DetectorLeaseController(backend, state_path, lambda: None)
    controller.acquire(OWNER)
    state = json.loads(state_path.read_text())
    state["heartbeat_at"] = time.time() - main.LEASE_STALE_SECONDS - 1
    state_path.write_text(json.dumps(state))
    controller.recover_stale()
    assert backend.start_calls == ["detector-id"]
    assert not state_path.exists()


def test_removed_owned_container_does_not_start_replacement(tmp_path: Path) -> None:
    backend = FakeBackend()
    controller = main.DetectorLeaseController(backend, tmp_path / "state.json", lambda: OWNER)
    controller.acquire(OWNER)
    backend.exists = False
    controller.release(OWNER)
    assert backend.start_calls == []


def test_recovery_defers_while_another_gpu_tenant_holds_lease(tmp_path: Path) -> None:
    backend = FakeBackend()
    holders = [OTHER, None]
    controller = main.DetectorLeaseController(
        backend, tmp_path / "state.json", lambda: holders.pop(0)
    )
    controller.acquire(OWNER)
    deferred = controller.release(OWNER)
    assert deferred["state"] == "recovery_deferred"
    assert backend.start_calls == []
    restored = controller.release(OWNER)
    assert restored["restored"] is True
    assert backend.start_calls == ["detector-id"]


@pytest.mark.parametrize("summary_state", ["paused", "restarting"])
def test_active_non_running_detector_states_are_stopped(tmp_path: Path, summary_state: str) -> None:
    backend = FakeBackend(running=True, summary_state=summary_state)
    controller = main.DetectorLeaseController(backend, tmp_path / "state.json", lambda: OWNER)
    state = controller.acquire(OWNER)
    assert state["stopped_by_controller"] is True
    assert state["detector_state_before"] == summary_state
    assert backend.stop_calls == ["detector-id"]


@pytest.mark.parametrize("holder", [OWNER, main._GPU_LEASE_UNAVAILABLE])
def test_stale_recovery_defers_for_same_or_unknown_gpu_holder(tmp_path: Path, holder: str) -> None:
    backend = FakeBackend()
    state_path = tmp_path / "state.json"
    controller = main.DetectorLeaseController(backend, state_path, lambda: holder)
    controller.acquire(OWNER)
    state = json.loads(state_path.read_text())
    state["heartbeat_at"] = time.time() - main.LEASE_STALE_SECONDS - 1
    state_path.write_text(json.dumps(state))
    recovered = controller.recover_stale()
    assert recovered and recovered["state"] == "recovery_deferred"
    assert backend.start_calls == []
    assert state_path.exists()


def test_controller_authentication_rejects_missing_or_wrong_token() -> None:
    token = "c" * 32
    assert main._authorized(token, token) is True
    assert main._authorized(None, token) is False
    assert main._authorized("wrong", token) is False
    assert main._authorized(token, "") is False


def test_lifecycle_endpoints_reject_unauthenticated_requests(monkeypatch) -> None:
    token = "c" * 32
    monkeypatch.setattr(main, "CONTROLLER_TOKEN", token)
    server = ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        body = json.dumps({"job_id": OWNER})
        connection.request(
            "POST",
            "/v1/detector/lease/acquire",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        assert connection.getresponse().status == 401
        connection.close()

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request(
            "POST",
            "/v1/detector/lease/release",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-Scarguard-Controller-Token": "wrong",
            },
        )
        assert connection.getresponse().status == 401
        connection.close()

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/v1/detector/lease")
        assert connection.getresponse().status == 401
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_invalid_owner_rejected_before_docker_access(tmp_path: Path) -> None:
    controller = main.DetectorLeaseController(FakeBackend(), tmp_path / "state.json", lambda: OWNER)
    with pytest.raises(main.ControllerError):
        controller.acquire("../../detector")
