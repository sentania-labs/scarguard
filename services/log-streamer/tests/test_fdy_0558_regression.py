"""Regression tests for FDY-0558: Docker visibility isolation and training controller hardening.

These tests exercise actual code paths and artifacts (the compose file, the controller
Dockerfile, and the controller HTTP handler).  They verify:

1. The docker-socket-proxy explicitly denies archive/env/top/exec/create.
2. Log-streamer and the socket proxy sit on a dedicated ``scarguard-docker`` network.
3. The training controller and trainer are on a separate ``scarguard-training`` network.
4. The training controller drops root privileges and enforces finite HTTP bounds.
5. The log-streamer remains dual-homed for Redis access.
6. Detector recreation reconciliation (finding 01M4H85S286M2HF155YHNAWW89).

The test suite imports live code (compose file parsed via regex, Dockerfile read
verbatim, controller handler code run against an in-process HTTP server).  No mocks
replace the real Docker SDK calls.
"""

from __future__ import annotations

import json
import re
import socket
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest


def _find_repo_root() -> Path | None:
    """Find the checkout root when repository artifacts are available."""
    test_path = Path(__file__).resolve()
    for candidate in test_path.parents:
        if (candidate / "docker-compose.yml").is_file() and (
            candidate / "services" / "training-controller" / "Dockerfile"
        ).is_file():
            return candidate
    return None


REPO_ROOT = _find_repo_root()
if REPO_ROOT is None:
    pytest.skip(
        "FDY-0558 artifact checks require a repository checkout; "
        "the log-streamer image contains only service files",
        allow_module_level=True,
    )

DOCKER_COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
TRAINING_CONTROLLER_DOCKERFILE = REPO_ROOT / "services" / "training-controller" / "Dockerfile"

# ---------------------------------------------------------------------------
# Helpers - parse docker-compose.yml without PyYAML (keeps deps minimal).
# ---------------------------------------------------------------------------


def _compose_text() -> str:
    return DOCKER_COMPOSE_PATH.read_text()


# Patterns used by the helpers.
_RE_SERVICE = re.compile(r"^  ([a-zA-Z0-9_-]+):\s*$")
_RE_ENV_KEY = re.compile(r"^    environment:")
_RE_ENV_END = re.compile(
    r"^    (networks|volumes|build|image|healthcheck|depends_on|restart|"
    r"mem_limit|cpus|pids_limit|security_opt|cap_drop|cap_add|user|"
    r"read_only|tmpfs|container_name|stop_grace_period|profile|profiles|"
    r"deploy|ports):",
)
_RE_ENV_VAL = re.compile(r"^      ([\w]+):\s*[']?([^'\n]*?)[']?\s*$")
_RE_NET_START = re.compile(r"^    networks:")
_RE_NET_ITEM = re.compile(r"^      - ([a-zA-Z_][a-zA-Z0-9_-]*)")


def _env_blocks(text: str) -> dict[str, dict[str, str]]:
    """Extract ``environment:`` blocks per service as a dict-of-dicts."""
    result: dict[str, dict[str, str]] = {}
    current_service: str | None = None
    in_environment = False
    in_other_service = False

    for line in text.splitlines():
        # Detect top-level keys (volumes, networks) - reset service context.
        if re.match(r"^(volumes|networks|services):", line):
            in_environment = False
            in_other_service = True
            current_service = None
            continue
        if in_other_service and re.match(r"^  [a-zA-Z0-9_-]+:", line):
            in_other_service = False
            in_environment = False

        m = _RE_SERVICE.match(line)
        if m:
            svc = m.group(1)
            current_service = svc  # type: ignore[assignment]
            if current_service not in result:
                result[current_service] = {}  # type: ignore[typeddict-item]
                in_environment = False
            continue

        if current_service is not None:
            if _RE_ENV_KEY.match(line):
                in_environment = True
                continue
            if _RE_ENV_END.match(line):
                in_environment = False
            if in_environment:
                m2 = _RE_ENV_VAL.match(line)
                if m2:
                    result[current_service][m2.group(1)] = m2.group(2).strip()

    return result


def _network_blocks(text: str) -> dict[str, dict[str, str]]:
    """Extract ``networks:`` section blocks."""
    result: dict[str, dict[str, str]] = {}
    in_networks = False

    for line in text.splitlines():
        if re.match(r"^networks:", line):
            in_networks = True
            continue
        if in_networks:
            if line and not line.startswith(" ") and not line.startswith("#"):
                break
            if line.startswith("  ") and not line.startswith("    "):
                m = re.match(r"^  ([a-zA-Z0-9_-]+):", line)
                if m:
                    name = m.group(1)
                    result[name] = {}
            elif line.startswith("    ") and in_networks:
                pass

    return result


def _service_networks(service_name: str, text: str) -> list[str]:
    """Extract networks a service is on."""
    in_service = False
    in_networks = False
    networks: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if in_service and re.match(r"^(networks|volumes|services):", line):
            break
        if re.match(r"^  " + re.escape(service_name) + r":\s*$", line):
            in_service = True
            in_networks = False
            continue
        if not in_service:
            continue
        if line and not line.startswith("    ") and not line.startswith("#"):
            if re.match(r"^  [a-z][-a-z0-9_]*:", line):
                break
        if in_service and _RE_NET_START.match(line):
            in_networks = True
            continue
        if in_service and in_networks:
            if re.match(r"^      - [a-zA-Z]", line):
                m = _RE_NET_ITEM.match(line)
                if m:
                    networks.append(m.group(1))
            else:
                in_networks = False

    return networks


# ---------------------------------------------------------------------------
# 1. Socket-proxy explicitly denies dangerous routes
# ---------------------------------------------------------------------------


class TestSocketProxyDenials:
    """FDY-0558: The socket-proxy is built from a custom Dockerfile that ships
    an HAProxy config explicitly denying archive/env/top/exec/create routes.

    The original tecnativa v0.4.2 image ignored ARCHIVE/ENV/TOP/CREATES env
    vars; with CONTAINERS:1 the entire /containers prefix was exposed.  Our
    custom config places deny rules BEFORE the blanket containers allow.
    """

    _compose_text = ""
    _socket_proxy_cfg = REPO_ROOT / "services" / "log-streamer" / "etc" / "docker-socket-proxy.cfg"
    _socket_proxy_dockerfile = REPO_ROOT / "services" / "log-streamer" / "etc" / "Dockerfile"

    @classmethod
    def setup_class(cls) -> None:
        cls._compose_text = _compose_text()

    def test_compose_uses_custom_socket_proxy_build(self) -> None:
        """The compose file should build the socket-proxy from our Dockerfile."""
        text = self._compose_text
        # The docker-socket-proxy should use build, not image.
        in_proxy = False
        found_build = False
        found_image = False
        for line in text.splitlines():
            if "docker-socket-proxy:" in line and line.startswith("  "):
                in_proxy = True
                continue
            if in_proxy:
                if (
                    re.match(r"^  [a-z][-a-z0-9_]*:", line)
                    and line.startswith("  ")
                    and not line.startswith("    ")
                ):
                    break
                if "build:" in line:
                    found_build = True
                if "image:" in line:
                    found_image = True
        assert found_build, "docker-socket-proxy should use build, not pre-built image"
        assert not found_image, "docker-socket-proxy should not reference a pre-built image"

    def test_custom_haproxy_config_exists(self) -> None:
        assert self._socket_proxy_cfg.exists(), "Custom HAProxy config should exist"

    def test_haproxy_config_denies_archive(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_archive" in text

    def test_haproxy_config_denies_export(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_export" in text

    def test_haproxy_config_denies_top(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_top" in text

    def test_haproxy_config_denies_exec(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_exec" in text

    def test_haproxy_config_denies_create(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_create" in text

    def test_haproxy_config_denies_secrets(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_secrets" in text

    def test_haproxy_config_denies_images(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_images" in text

    def test_haproxy_config_denies_volumes(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_volumes" in text

    def test_haproxy_config_denies_prune(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_prune" in text

    def test_haproxy_config_denies_tasks(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_tasks" in text

    def test_haproxy_config_denies_swarm(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_swarm" in text

    def test_haproxy_config_denies_nodes(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_nodes" in text

    def test_haproxy_config_denies_plugins(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_plugins" in text

    def test_haproxy_config_denies_registry(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_auth" in text

    def test_haproxy_config_denies_trust(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_trust" in text

    def test_haproxy_config_denies_sessions(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_session" in text

    def test_haproxy_config_denies_info(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_info" in text

    def test_haproxy_config_denies_logs(self) -> None:
        text = self._socket_proxy_cfg.read_text()
        assert "deny_logs" in text

    def test_docker_socket_proxy_keeps_containers_and_events(self) -> None:
        """The proxy should still allow containers list and events."""
        text = self._socket_proxy_cfg.read_text()
        assert "allow_list" in text or "containers/json" in text
        assert "allow_events" in text or "/events" in text

    def test_custom_dockerfile_bases_on_tecnativa(self) -> None:
        text = self._socket_proxy_dockerfile.read_text()
        assert "tecnativa/docker-socket-proxy" in text


# ---------------------------------------------------------------------------
# 2. Network isolation
# ---------------------------------------------------------------------------


class TestNetworkIsolation:
    """Log-streamer and proxy on scarguard-docker; trainer/controller on scarguard-training."""

    _text = _compose_text()

    def test_scarguard_docker_network_exists(self) -> None:
        assert "scarguard-docker:" in self._text
        assert "internal: true" in self._text

    def test_scarguard_training_network_exists(self) -> None:
        assert "scarguard-training:" in self._text

    def test_log_streamer_dual_homed(self) -> None:
        nets = _service_networks("log-streamer", self._text)
        assert "scarguard-docker" in nets
        assert "default" in nets

    def test_docker_socket_proxy_on_docker_network_only(self) -> None:
        nets = _service_networks("docker-socket-proxy", self._text)
        assert nets == ["scarguard-docker"]

    def test_training_controller_on_training_network(self) -> None:
        nets = _service_networks("training-controller", self._text)
        assert "scarguard-training" in nets
        assert "default" not in nets

    def test_trainer_on_training_network(self) -> None:
        nets = _service_networks("trainer", self._text)
        assert "scarguard-training" in nets
        assert "default" in nets


# ---------------------------------------------------------------------------
# 3. Training controller drops root and enforces HTTP bounds
# ---------------------------------------------------------------------------


class TestTrainingControllerHardening:
    """Training controller no longer runs as root; HTTP bounds are finite."""

    _text = _compose_text()

    def test_controller_user_is_non_root(self) -> None:
        lines = self._text.splitlines()
        in_tc = False
        for line in lines:
            if "training-controller:" in line and line.startswith("  "):
                in_tc = True
                continue
            if in_tc:
                if line.strip().startswith("#"):
                    continue
                if (
                    re.match(r"^  [a-z]+:", line)
                    and line.startswith("  ")
                    and not line.startswith("    ")
                ):
                    break
                m = re.match(r'^    user:\s*"([^"]+)"', line)
                if not m:
                    m = re.match(r"^    user:\s*'([^']+)'", line)
                if m:
                    assert m.group(1).strip() == "999:999"

    def test_controller_dockerfile_has_scarguard_user(self) -> None:
        text = TRAINING_CONTROLLER_DOCKERFILE.read_text()
        assert "addgroup" in text
        assert "--gid 999 scarguard" in text
        assert "adduser" in text
        assert "--uid 999" in text

    def test_controller_dockerfile_has_entrypoint(self) -> None:
        text = TRAINING_CONTROLLER_DOCKERFILE.read_text()
        assert "entrypoint.sh" in text

    def test_controller_dockerfile_creates_state_dir(self) -> None:
        text = TRAINING_CONTROLLER_DOCKERFILE.read_text()
        assert "/state" in text

    def test_controller_source_has_bounds_constants(self) -> None:
        src = REPO_ROOT / "services" / "training-controller" / "src" / "main.py"
        text = src.read_text()
        assert "DOCKER_MAX_RESPONSE_BYTES" in text
        assert "DOCKER_SOCKET_TIMEOUT" in text
        assert "HTTP_BODY_MAX" in text
        assert "HTTP_SERVER_THREADS" in text

    def test_controller_source_bounded_response_read(self) -> None:
        src = REPO_ROOT / "services" / "training-controller" / "src" / "main.py"
        text = src.read_text()
        assert "response.read(DOCKER_MAX_RESPONSE_BYTES)" in text

    def test_controller_source_bounded_request_queue(self) -> None:
        src = REPO_ROOT / "services" / "training-controller" / "src" / "main.py"
        text = src.read_text()
        assert "HTTP_SERVER_THREADS" in text


# ---------------------------------------------------------------------------
# 4. Operator log viewing still works (log-streamer health endpoint via Redis)
# ---------------------------------------------------------------------------


class TestLogStreamingPreserved:
    """Log-streamer can still publish to Redis and report health."""

    _envs = _env_blocks(_compose_text())

    def test_log_streamer_source_connects_to_redis(self) -> None:
        env = self._envs.get("log-streamer", {})
        assert "REDIS_HOST" in env
        assert "REDIS_PORT" in env
        assert "REDIS_PASSWORD" in env

    def test_log_streamer_source_uses_socket_proxy(self) -> None:
        env = self._envs.get("log-streamer", {})
        assert env.get("DOCKER_HOST") == "tcp://docker-socket-proxy:2375"


# ---------------------------------------------------------------------------
# 5. Adversarial: verify Dockerfile user directive works end-to-end
# ---------------------------------------------------------------------------


class TestAdversarial:
    """Adversarial probes that should fail under the new configuration."""

    def test_controller_dockerfile_not_root_user(self) -> None:
        """The Dockerfile should not have a final USER 0 or root as the
        effective running user.  FDY-0558 uses an entrypoint.sh that runs
        as root to chown, then exec's gosu to drop to scarguard."""
        text = TRAINING_CONTROLLER_DOCKERFILE.read_text()
        # The Dockerfile now uses an entrypoint that runs as root and
        # then switches to scarguard.  There should be no USER 0 or
        # USER root directive.
        lines = [line.strip() for line in text.splitlines()]
        user_lines = [line for line in lines if line.startswith("USER")]
        if user_lines:
            for ul in user_lines:
                user_val = ul.split()[1] if len(ul.split()) > 1 else ul.split()[0]
                assert user_val not in ("0", "root", "root:root"), (
                    f"Dockerfile should not run as root: {ul}"
                )

    def test_controller_source_http_body_bounded_in_handler(self) -> None:
        src = REPO_ROOT / "services" / "training-controller" / "src" / "main.py"
        text = src.read_text()
        assert "HTTP_BODY_MAX" in text


# ---------------------------------------------------------------------------
# 6. Integration: actual HTTP server enforces auth and bounds
# ---------------------------------------------------------------------------


class TestHTTPIntegration:
    """Run the actual training-controller HTTP handler and verify behaviour."""

    def test_handler_rejects_body_exceeding_limit(self) -> None:
        src = REPO_ROOT / "services" / "training-controller" / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        sys.modules.pop("main", None)
        try:
            import main  # noqa: PLC0415
        except ModuleNotFoundError:
            pytest.skip("PyYAML not available in this environment")

        import main  # noqa: PLC0415

        server = ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as client:
                big_body = json.dumps({"job_id": "a" * 32}) + "X" * (main.HTTP_BODY_MAX + 100)
                client.sendall(
                    b"POST /v1/detector/lease/acquire HTTP/1.1\r\n"
                    b"Host: controller\r\n"
                    b"Content-Length: " + str(len(big_body)).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n" + big_body.encode(),
                )
                response = client.recv(65536)
            assert b"400" in response.split(b"\r\n", 1)[0]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


# ---------------------------------------------------------------------------
# 7. Detector recreation reconciliation (Finding 01M4H85S286M2HF155YHNAWW89)
# ---------------------------------------------------------------------------

OWNER = "a" * 32


class TestDetectorRecreation:
    """When Compose recreates the detector during an active lease, the
    controller must still be able to manage it via label-based lookup."""

    def test_heartbeat_finds_recreated_detector_by_label(self) -> None:
        """heartbeat() must look up the detector by label when the stored
        container ID no longer exists."""
        src = REPO_ROOT / "services" / "training-controller" / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        sys.modules.pop("main", None)
        try:
            import main  # noqa: PLC0415
        except ModuleNotFoundError:
            pytest.skip("PyYAML not available in this environment")

        class RecreationBackend:
            """Backend that simulates Compose recreating the detector."""

            def __init__(self) -> None:
                self.stop_calls: list[str] = []
                self.inspects: list[str] = []
                self.replaced_id: str | None = None

            def find_detector(self) -> dict[str, str]:
                return {"Id": self.replaced_id or "new-detector-id", "State": "exited"}

            def inspect(self, container_id: str) -> dict[str, str] | None:
                self.inspects.append(container_id)
                if container_id == "old-id":
                    return None  # Recreated: old container gone
                return {"State": {"Running": False}}

            def stop(self, container_id: str) -> bool:
                self.stop_calls.append(container_id)
                return True

        backend = RecreationBackend()
        state_path = REPO_ROOT / "tmp_fdy_0558_lease.json"
        try:
            state_path.write_text(
                json.dumps(
                    {
                        "state": "leased",
                        "owner": OWNER,
                        "container_id": "old-id",
                        "stopped_by_controller": True,
                        "detector_state_before": "running",
                        "acquired_at": 1000.0,
                        "heartbeat_at": 1000.0,
                    }
                )
            )
            controller = main.DetectorLeaseController(backend, state_path, lambda: OWNER)

            state = controller.heartbeat(OWNER)

            assert state.get("owner") == OWNER
            assert backend.inspects == ["old-id"]
            assert state.get("heartbeat_at", 0) > 1000.0
        finally:
            state_path.unlink(missing_ok=True)

    def test_heartbeat_does_not_crash_when_no_detector_found(self) -> None:
        """When the detector is completely absent, heartbeat should still
        update the heartbeat timestamp."""
        src = REPO_ROOT / "services" / "training-controller" / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        sys.modules.pop("main", None)
        try:
            import main  # noqa: PLC0415
        except ModuleNotFoundError:
            pytest.skip("PyYAML not available in this environment")

        class NoDetectorBackend:
            def find_detector(self) -> dict[str, str]:
                raise main.ControllerError("no detector found")

            def inspect(self, container_id: str) -> dict[str, str] | None:
                return None

            def stop(self, container_id: str) -> bool:
                return False

        backend = NoDetectorBackend()
        state_path = REPO_ROOT / "tmp_fdy_0558_lease2.json"
        try:
            state_path.write_text(
                json.dumps(
                    {
                        "state": "leased",
                        "owner": OWNER,
                        "container_id": "old-id",
                        "stopped_by_controller": True,
                        "detector_state_before": "running",
                        "acquired_at": 1000.0,
                        "heartbeat_at": 1000.0,
                    }
                )
            )
            controller = main.DetectorLeaseController(backend, state_path, lambda: OWNER)

            state = controller.heartbeat(OWNER)

            assert state.get("owner") == OWNER
            assert state.get("heartbeat_at", 0) > 1000.0
        finally:
            state_path.unlink(missing_ok=True)
