"""ScarGuard log-streamer sidecar.

Tails Docker container logs and publishes them to Redis so the web UI
can stream logs without needing direct Docker socket access.

Each discovered Compose service gets a dedicated tail thread. Log lines
are published to ``scarguard:logs:{service}`` (pub/sub) and buffered in
``scarguard:logs:buffer:{service}`` (Redis list, newest-first, capped at
BUFFER_MAX) so clients can backfill historical lines on connect.
"""

from __future__ import annotations

import itertools
import logging
import os
import queue
import re
import signal
import threading
import time
from pathlib import Path
from types import FrameType
from typing import Callable

import docker
import redis as redislib
import yaml
from pydantic import BaseModel, Field, ValidationError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("log-streamer")

COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT", "scarguard")
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/config/scarguard.yml"))
CHANNEL_PREFIX = "scarguard:logs:"
BUFFER_PREFIX = "scarguard:logs:buffer:"
IDENTITY_PREFIX = "scarguard:logs:identity:"
HEALTH_EVENTS_KEY = "scarguard:logs:published:5m:events"
HEALTH_KEY = "scarguard:logs:published:5m:count"
BUFFER_MAX = 2000
BACKFILL_LINES = 100
HEALTH_WINDOW_SECONDS = 300
DISCOVERY_INTERVAL = 30
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mK]")
_health_member_sequence = itertools.count()
_HEALTH_UPDATE_SCRIPT = """
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[3])
local count = redis.call('ZCARD', KEYS[1])
redis.call('SET', KEYS[2], count, 'EX', ARGV[4])
return count
"""
_HEALTH_REFRESH_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local count = redis.call('ZCARD', KEYS[1])
if count > 0 then
    redis.call('SET', KEYS[2], count, 'EX', ARGV[2])
else
    redis.call('DEL', KEYS[2])
end
return count
"""

_stop = threading.Event()


class LogStreamerSettings(BaseModel):
    """Operator-controlled recovery thresholds."""

    quick_eof_limit: int = Field(default=3, ge=1, le=20)
    quick_eof_threshold_seconds: int = Field(default=10, ge=1, le=300)


class TailResult(BaseModel):
    """Completion report sent from a tail thread to the manager."""

    service: str
    elapsed_seconds: float
    stopped: bool


class ParsedLogLine(BaseModel):
    """One Docker log event with a stable reconnect identity."""

    text: str
    identity: str


DockerClientFactory = Callable[[], docker.DockerClient]
RedisClientFactory = Callable[[], redislib.Redis]


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def load_settings(config_path: Path = CONFIG_PATH) -> LogStreamerSettings:
    """Load log-streamer settings, falling back to safe working defaults."""
    try:
        with config_path.open() as config_file:
            loaded = yaml.safe_load(config_file) or {}
        if not isinstance(loaded, dict):
            raise ValueError("Config root must be a mapping")
        system = loaded.get("system", {})
        if not isinstance(system, dict):
            raise ValueError("Config system section must be a mapping")
        raw_settings = system.get("log_streamer", {})
        return LogStreamerSettings.model_validate(
            raw_settings if isinstance(raw_settings, dict) else {}
        )
    except FileNotFoundError:
        return LogStreamerSettings()
    except (OSError, ValueError, yaml.YAMLError, ValidationError) as exc:
        logger.warning("Invalid log-streamer config, using defaults: %s", exc)
        return LogStreamerSettings()


def _redis_client() -> redislib.Redis:
    host = os.environ.get("REDIS_HOST", "redis")
    port = int(os.environ.get("REDIS_PORT", "6379"))
    password = os.environ.get("REDIS_PASSWORD", "") or None
    return redislib.Redis(
        host=host,
        port=port,
        password=password,
        decode_responses=True,
        retry_on_timeout=True,
    )


def discover_services(
    client: docker.DockerClient,
) -> dict[str, docker.models.containers.Container]:
    """Return service names and containers for the running Compose project."""
    try:
        containers = client.containers.list(
            filters={"label": f"com.docker.compose.project={COMPOSE_PROJECT}"}
        )
    except Exception:
        logger.exception("Failed to list containers")
        return {}

    result: dict[str, docker.models.containers.Container] = {}
    for container in containers:
        service = container.labels.get("com.docker.compose.service")
        if service and service != "log-streamer":
            result[service] = container
    return result


def _recent_identities(
    client: redislib.Redis,
    identity_key: str,
) -> set[str]:
    """Return stable identities for the newest entries in the paired buffer."""
    try:
        return set(client.lrange(identity_key, 0, BACKFILL_LINES - 1))
    except redislib.RedisError:
        logger.warning("Redis identity lookup failed for %s", identity_key)
        return set()


def _parse_log_chunk(chunk: bytes | str, container_id: str) -> list[ParsedLogLine]:
    """Decode timestamped Docker output into individual log events."""
    decoded = (
        chunk.decode("utf-8", errors="replace")
        if isinstance(chunk, bytes)
        else str(chunk)
    )
    parsed: list[ParsedLogLine] = []
    for raw_line in decoded.splitlines():
        timestamp, separator, payload = raw_line.partition(" ")
        if not separator:
            timestamp = ""
            payload = raw_line
        clean = _strip_ansi(payload).rstrip("\r")
        if not clean:
            continue
        parsed.append(
            ParsedLogLine(
                text=clean,
                identity=f"{container_id}:{timestamp}:{clean}",
            )
        )
    return parsed


def _publish_line(
    client: redislib.Redis,
    channel: str,
    buffer_key: str,
    identity_key: str,
    service: str,
    log_line: ParsedLogLine,
) -> None:
    """Publish and buffer one log line while updating the rolling health key."""
    now = time.time()
    health_member = f"{time.time_ns()}:{next(_health_member_sequence)}:{service}"
    pipe = client.pipeline(transaction=False)
    pipe.publish(channel, log_line.text)
    pipe.lpush(buffer_key, log_line.text)
    pipe.ltrim(buffer_key, 0, BUFFER_MAX - 1)
    pipe.lpush(identity_key, log_line.identity)
    pipe.ltrim(identity_key, 0, BUFFER_MAX - 1)
    pipe.eval(
        _HEALTH_UPDATE_SCRIPT,
        2,
        HEALTH_EVENTS_KEY,
        HEALTH_KEY,
        now,
        health_member,
        now - HEALTH_WINDOW_SECONDS,
        HEALTH_WINDOW_SECONDS,
    )
    pipe.execute()


def refresh_health(
    redis_factory: RedisClientFactory = _redis_client,
    now: float | None = None,
) -> int:
    """Prune expired health events and refresh the published-line count."""
    redis_client = redis_factory()
    current_time = time.time() if now is None else now
    try:
        result = redis_client.eval(
            _HEALTH_REFRESH_SCRIPT,
            2,
            HEALTH_EVENTS_KEY,
            HEALTH_KEY,
            current_time - HEALTH_WINDOW_SECONDS,
            HEALTH_WINDOW_SECONDS,
        )
        return int(result)
    except redislib.RedisError:
        logger.warning("Redis health refresh failed")
        return 0
    finally:
        redis_client.close()


def tail_container(
    service: str,
    container: docker.models.containers.Container,
    result_callback: Callable[[TailResult], None],
    redis_factory: RedisClientFactory = _redis_client,
) -> None:
    """Tail one container and publish its logs. Runs in a thread."""
    channel = f"{CHANNEL_PREFIX}{service}"
    buffer_key = f"{BUFFER_PREFIX}{service}"
    identity_key = f"{IDENTITY_PREFIX}{service}"
    logger.info("Tailing %s (container %s)", service, container.short_id)

    started = time.monotonic()
    redis_client = redis_factory()
    known_identities = _recent_identities(redis_client, identity_key)
    attach_since = max(0, int(time.time()) - 1)

    def publish_new(chunk: bytes | str) -> None:
        for log_line in _parse_log_chunk(chunk, container.id):
            if log_line.identity in known_identities:
                continue
            try:
                _publish_line(
                    redis_client,
                    channel,
                    buffer_key,
                    identity_key,
                    service,
                    log_line,
                )
                known_identities.add(log_line.identity)
            except redislib.RedisError:
                logger.warning("Redis publish failed for %s, will retry", service)

    try:
        backfill = container.logs(
            stream=False,
            follow=False,
            tail=BACKFILL_LINES,
            timestamps=True,
        )
        if backfill:
            publish_new(backfill)

        for chunk in container.logs(
            stream=True,
            follow=True,
            tail=0,
            since=attach_since,
            timestamps=True,
        ):
            if _stop.is_set():
                break
            publish_new(chunk)
    except Exception:
        if not _stop.is_set():
            logger.warning("Log stream ended for %s", service, exc_info=True)
    finally:
        elapsed = time.monotonic() - started
        stopped = _stop.is_set()
        redis_client.close()
        result_callback(
            TailResult(
                service=service,
                elapsed_seconds=elapsed,
                stopped=stopped,
            )
        )
        logger.info("Tail thread exiting for %s", service)


class LogStreamer:
    """Own Docker discovery, tail threads, and stale-client recovery."""

    def __init__(
        self,
        client_factory: DockerClientFactory = docker.from_env,
        redis_factory: RedisClientFactory = _redis_client,
    ) -> None:
        self._client_factory = client_factory
        self._redis_factory = redis_factory
        self.docker_client = client_factory()
        self.active: dict[str, tuple[threading.Thread, str]] = {}
        self.tail_results: queue.Queue[TailResult] = queue.Queue()
        self.consecutive_quick_eofs = 0

    def _replace_docker_client(self) -> None:
        """Replace the Docker SDK client after repeated short-lived streams."""
        try:
            replacement = self._client_factory()
        except Exception:
            logger.exception("Failed to recreate Docker client")
            return
        previous = self.docker_client
        self.docker_client = replacement
        previous.close()
        self.consecutive_quick_eofs = 0
        logger.warning("Recreated Docker client after repeated quick log stream EOFs")

    def _process_tail_results(self, settings: LogStreamerSettings) -> None:
        """Consume thread results and heal the client at the configured limit."""
        while True:
            try:
                result = self.tail_results.get_nowait()
            except queue.Empty:
                break
            if result.stopped:
                continue
            if result.elapsed_seconds < settings.quick_eof_threshold_seconds:
                self.consecutive_quick_eofs += 1
            else:
                self.consecutive_quick_eofs = 0

        if self.consecutive_quick_eofs >= settings.quick_eof_limit:
            self._replace_docker_client()

    def run_cycle(self, settings: LogStreamerSettings) -> None:
        """Run one discovery and reconciliation cycle."""
        refresh_health(self._redis_factory)
        self._process_tail_results(settings)
        services = discover_services(self.docker_client)

        for service in list(self.active):
            if service not in services:
                logger.info("Service %s gone, stopping tail", service)
                del self.active[service]

        for service, container in services.items():
            if service in self.active:
                thread, old_id = self.active[service]
                if container.id != old_id:
                    logger.info("Service %s restarted, restarting tail", service)
                    del self.active[service]
                elif not thread.is_alive():
                    logger.debug("Tail thread for %s died, respawning", service)
                    del self.active[service]

        for service, container in services.items():
            if service in self.active:
                continue
            thread = threading.Thread(
                target=tail_container,
                args=(service, container, self.tail_results.put, self._redis_factory),
                name=f"tail-{service}",
                daemon=True,
            )
            thread.start()
            self.active[service] = (thread, container.id)

    def close(self) -> None:
        """Close manager resources during process shutdown."""
        self.docker_client.close()


def main() -> None:
    logger.info("Starting log-streamer sidecar")
    _stop.clear()
    streamer = LogStreamer()

    def _shutdown(signum: int, _frame: FrameType | None) -> None:
        logger.info("Received signal %d, shutting down", signum)
        _stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while not _stop.is_set():
        streamer.run_cycle(load_settings())
        _stop.wait(DISCOVERY_INTERVAL)

    logger.info("Shutting down, waiting for tail threads")
    streamer.close()
    logger.info("Log-streamer stopped")


if __name__ == "__main__":
    main()
