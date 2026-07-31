"""Allowlisted detector lifecycle controller for training GPU isolation.

This is the only application-side service with Docker write access. Its API
contains no arbitrary container name or Docker operation: callers can acquire,
heartbeat, release, or recover a lease for the single Compose detector service.
Ownership is persisted before a stop so crash recovery never starts a detector
that this controller did not stop.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode

logger = logging.getLogger(__name__)

SOCKET_PATH = os.environ.get("DOCKER_SOCKET_PATH", "/var/run/docker.sock")
STATE_PATH = Path(os.environ.get("CONTROLLER_STATE_PATH", "/state/detector-lease.json"))
LEASE_STALE_SECONDS = int(os.environ.get("CONTROLLER_LEASE_STALE_SECONDS", "120"))
DETECTOR_SERVICE_LABEL = "com.docker.compose.service=detector"
DETECTOR_PROJECT_LABEL = "com.docker.compose.project=" + os.environ.get(
    "DETECTOR_COMPOSE_PROJECT", "scarguard"
)
GPU_LEASE_KEY = "scarguard:trainer:heartbeat"
CONTROLLER_TOKEN = os.environ.get("TRAINING_CONTROLLER_TOKEN", "")
_GPU_LEASE_UNAVAILABLE = "__redis_unavailable__"
_OWNER_RE = re.compile(r"^[a-f0-9]{32}$")
_ACTIVE_SUMMARY_STATES = {"running", "paused", "restarting"}


class ControllerError(RuntimeError):
    """Expected lifecycle-controller error with an HTTP status."""

    def __init__(self, message: str, status: int = HTTPStatus.CONFLICT) -> None:
        super().__init__(message)
        self.status = status


class DockerBackend:
    """Minimal Docker Engine client exposing only operations needed here."""

    def _request(self, method: str, path: str) -> tuple[int, bytes]:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(30)
            sock.connect(SOCKET_PATH)
            request = (
                f"{method} {path} HTTP/1.1\r\n"
                "Host: docker\r\n"
                "Connection: close\r\n"
                "Content-Length: 0\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            sock.close()

        raw = b"".join(chunks)
        head, _, body = raw.partition(b"\r\n\r\n")
        first = head.splitlines()[0].split()
        if len(first) < 2:
            raise ControllerError("Invalid response from Docker Engine", HTTPStatus.BAD_GATEWAY)
        return int(first[1]), body

    def find_detector(self) -> dict[str, Any]:
        filters = json.dumps({"label": [DETECTOR_SERVICE_LABEL, DETECTOR_PROJECT_LABEL]})
        path = "/v1.41/containers/json?" + urlencode({"all": "1", "filters": filters})
        status, body = self._request("GET", path)
        if status != HTTPStatus.OK:
            raise ControllerError("Unable to inspect detector container", HTTPStatus.BAD_GATEWAY)
        containers = json.loads(body or b"[]")
        if len(containers) != 1:
            raise ControllerError(
                f"Expected exactly one Compose detector container, found {len(containers)}"
            )
        return containers[0]

    def inspect(self, container_id: str) -> dict[str, Any] | None:
        status, body = self._request("GET", f"/v1.41/containers/{quote(container_id)}/json")
        if status == HTTPStatus.NOT_FOUND:
            return None
        if status != HTTPStatus.OK:
            raise ControllerError("Unable to inspect owned detector", HTTPStatus.BAD_GATEWAY)
        return json.loads(body)

    def stop(self, container_id: str) -> bool:
        status, _ = self._request("POST", f"/v1.41/containers/{quote(container_id)}/stop?t=30")
        if status == HTTPStatus.NO_CONTENT:
            return True
        if status == HTTPStatus.NOT_MODIFIED:
            return False
        raise ControllerError("Docker Engine failed to stop detector", HTTPStatus.BAD_GATEWAY)

    def start(self, container_id: str) -> None:
        status, _ = self._request("POST", f"/v1.41/containers/{quote(container_id)}/start")
        if status not in (HTTPStatus.NO_CONTENT, HTTPStatus.NOT_MODIFIED):
            raise ControllerError("Docker Engine failed to start detector", HTTPStatus.BAD_GATEWAY)


def _gpu_lease_holder() -> str | None:
    """Read the shared Redis lease without adding a general Redis dependency."""
    host = os.environ.get("REDIS_HOST", "redis")
    port = int(os.environ.get("REDIS_PORT", "6379"))
    password = os.environ.get("REDIS_PASSWORD", "")

    def _command(stream: Any, *parts: str) -> bytes | None:
        payload = f"*{len(parts)}\r\n" + "".join(
            f"${len(part.encode())}\r\n{part}\r\n" for part in parts
        )
        stream.write(payload.encode())
        stream.flush()
        prefix = stream.read(1)
        if prefix == b"$":
            length = int(stream.readline().strip())
            if length < 0:
                return None
            value = stream.read(length)
            stream.read(2)
            return value
        line = stream.readline().strip()
        if prefix in (b"+", b":"):
            return line
        raise OSError(f"Redis error: {line.decode(errors='replace')}")

    try:
        with socket.create_connection((host, port), timeout=1) as client:
            stream = client.makefile("rwb")
            if password:
                _command(stream, "AUTH", password)
            value = _command(stream, "GET", GPU_LEASE_KEY)
            return value.decode(errors="replace") if value else None
    except OSError:
        logger.warning("Could not read shared GPU lease during detector recovery")
        # Recovery is fail-closed: an unavailable lease store is not evidence
        # that the GPU is free.
        return _GPU_LEASE_UNAVAILABLE


class DetectorLeaseController:
    """Persistent, ownership-aware stop/start state machine."""

    def __init__(
        self,
        backend: DockerBackend,
        state_path: Path = STATE_PATH,
        lease_holder: Callable[[], str | None] | None = None,
    ) -> None:
        self.backend = backend
        self.state_path = state_path
        self.lease_holder = lease_holder or _gpu_lease_holder
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.state_path.read_text())
            return data if isinstance(data, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _write(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True))
        os.replace(temporary, self.state_path)

    def _clear(self) -> None:
        try:
            self.state_path.unlink()
        except FileNotFoundError:
            pass

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._read() or {"state": "idle"}

    def acquire(self, owner: str) -> dict[str, Any]:
        _validate_owner(owner)
        with self._lock:
            current = self._read()
            if current:
                if current.get("owner") == owner:
                    return current
                raise ControllerError(
                    f"Detector lease is owned by {current.get('owner', 'unknown')}"
                )

            detector = self.backend.find_detector()
            container_id = str(detector["Id"])
            was_active = str(detector.get("State", "")).lower() in _ACTIVE_SUMMARY_STATES
            now = time.time()
            state = {
                "state": "stopping" if was_active else "leased",
                "owner": owner,
                "container_id": container_id,
                "stopped_by_controller": was_active,
                "detector_state_before": str(detector.get("State", "unknown")),
                "acquired_at": now,
                "heartbeat_at": now,
            }
            # Persist ownership before the destructive operation. Recovery may
            # safely start this exact container if the controller dies here.
            self._write(state)
            if was_active:
                try:
                    stopped_by_call = self.backend.stop(container_id)
                    if not stopped_by_call:
                        # Docker says it was already stopped. An operator may
                        # have won the race after discovery, so withdraw the
                        # provisional ownership before any later failure.
                        state["stopped_by_controller"] = False
                        self._write(state)
                    inspected = self.backend.inspect(container_id)
                    inspected_state = (inspected or {}).get("State", {})
                    if inspected is not None and (
                        inspected_state.get("Running", False)
                        or inspected_state.get("Paused", False)
                        or inspected_state.get("Restarting", False)
                    ):
                        raise ControllerError("Detector remained active after stop")
                except Exception:
                    # Keep the ownership record: Docker may have completed the
                    # stop even when the response was lost. The recovery loop
                    # can inspect and restore this exact container safely.
                    raise
                state["state"] = "leased"
                self._write(state)
            logger.info(
                "Detector lease acquired owner=%s stopped_by_controller=%s",
                owner,
                state["stopped_by_controller"],
            )
            return state

    def heartbeat(self, owner: str) -> dict[str, Any]:
        _validate_owner(owner)
        with self._lock:
            state = self._read()
            if not state or state.get("owner") != owner:
                raise ControllerError("Detector lease is not owned by this job")
            if state.get("stopped_by_controller"):
                owned = self.backend.inspect(str(state.get("container_id", "")))
                if owned is not None and owned.get("State", {}).get("Running", False):
                    logger.warning(
                        "Owned detector restarted during active lease; stopping it again"
                    )
                    self.backend.stop(str(state["container_id"]))
            state["heartbeat_at"] = time.time()
            self._write(state)
            return state

    def release(self, owner: str) -> dict[str, Any]:
        _validate_owner(owner)
        with self._lock:
            return self._release_locked(owner)

    def _release_locked(
        self, owner: str, *, defer_for_any_gpu_holder: bool = False
    ) -> dict[str, Any]:
        state = self._read()
        if not state:
            return {"state": "idle", "restored": False}
        if state.get("owner") != owner:
            raise ControllerError("Refusing to release another job's detector lease")

        restored = False
        if state.get("stopped_by_controller"):
            holder = self.lease_holder()
            if holder and (defer_for_any_gpu_holder or holder != owner):
                state["state"] = "recovery_deferred"
                state["deferred_for_gpu_lease"] = holder
                self._write(state)
                logger.warning("Detector restore deferred: GPU lease is held by %s", holder)
                return {"state": "recovery_deferred", "restored": False}
            owned_id = str(state.get("container_id", ""))
            detector = self.backend.inspect(owned_id)
            if detector is not None and not detector.get("State", {}).get("Running", False):
                self.backend.start(owned_id)
                restored = True
        self._clear()
        logger.info("Detector lease released owner=%s restored=%s", owner, restored)
        return {"state": "idle", "restored": restored}

    def recover(self, owner: str) -> dict[str, Any]:
        """Recover a known stale trainer job immediately on trainer startup."""
        _validate_owner(owner)
        with self._lock:
            return self._release_locked(owner, defer_for_any_gpu_holder=True)

    def recover_stale(self) -> dict[str, Any] | None:
        with self._lock:
            state = self._read()
            if not state:
                return None
            heartbeat_at = float(state.get("heartbeat_at", 0))
            if time.time() - heartbeat_at <= LEASE_STALE_SECONDS:
                return None
            owner = str(state.get("owner", ""))
            logger.warning("Recovering stale detector lease owner=%s", owner)
            return self._release_locked(owner, defer_for_any_gpu_holder=True)


def _validate_owner(owner: str) -> None:
    if not _OWNER_RE.fullmatch(owner):
        raise ControllerError(
            "Lease owner must be a 32-character lowercase job id", HTTPStatus.BAD_REQUEST
        )


def _authorized(presented: str | None, expected: str | None = None) -> bool:
    """Authenticate privileged lifecycle calls without leaking token length."""
    expected = CONTROLLER_TOKEN if expected is None else expected
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


controller = DetectorLeaseController(DockerBackend())


class Handler(BaseHTTPRequestHandler):
    server_version = "scarguard-training-controller/1"

    def log_message(self, fmt: str, *args: object) -> None:
        logger.info("controller http: " + fmt, *args)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _owner(self) -> str:
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            raise ControllerError("Invalid JSON request", HTTPStatus.BAD_REQUEST) from exc
        owner = str(payload.get("job_id", ""))
        _validate_owner(owner)
        return owner

    def _require_auth(self) -> bool:
        if _authorized(self.headers.get("X-Scarguard-Controller-Token")):
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(HTTPStatus.OK, {"status": "ok"})
        elif self.path == "/v1/detector/lease":
            if self._require_auth():
                self._json(HTTPStatus.OK, controller.state())
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        actions = {
            "/v1/detector/lease/acquire": controller.acquire,
            "/v1/detector/lease/heartbeat": controller.heartbeat,
            "/v1/detector/lease/release": controller.release,
            "/v1/detector/lease/recover": controller.recover,
        }
        action = actions.get(self.path)
        if action is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._require_auth():
            return
        try:
            self._json(HTTPStatus.OK, action(self._owner()))
        except ControllerError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception:
            logger.exception("Detector lifecycle operation failed")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "lifecycle operation failed"})


def _recovery_loop(stop: threading.Event) -> None:
    while not stop.wait(15):
        try:
            controller.recover_stale()
        except Exception:
            logger.exception("Stale detector lease recovery failed")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )
    if len(CONTROLLER_TOKEN) < 32:
        raise RuntimeError("TRAINING_CONTROLLER_TOKEN must contain at least 32 characters")
    stop = threading.Event()
    recovery = threading.Thread(target=_recovery_loop, args=(stop,), daemon=True)
    recovery.start()
    server = ThreadingHTTPServer(("0.0.0.0", 8090), Handler)
    try:
        server.serve_forever()
    finally:
        stop.set()
        recovery.join(timeout=5)


if __name__ == "__main__":
    main()
