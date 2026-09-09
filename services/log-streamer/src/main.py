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
from collections import deque
from datetime import datetime
from types import FrameType
from typing import Callable

import docker
import redis as redislib
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("log-streamer")

COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT", "scarguard")
CHANNEL_PREFIX = "scarguard:logs:"
BUFFER_PREFIX = "scarguard:logs:buffer:"
IDENTITY_PREFIX = "scarguard:logs:identity:"
IDENTITY_MIGRATION_PREFIX = "scarguard:logs:identity-migration:"
HEALTH_EVENTS_KEY = "scarguard:logs:published:5m:events"
HEALTH_KEY = "scarguard:logs:published:5m:count"
HEALTH_STATUS_KEY = "scarguard:logs:health"
BUFFER_MAX = 2000
BACKFILL_LINES = 100
HEALTH_WINDOW_SECONDS = 300
DISCOVERY_INTERVAL = 30
QUICK_EOF_LIMIT = 3
QUICK_EOF_THRESHOLD_SECONDS = 10
HEALTH_STATUS_TTL_SECONDS = DISCOVERY_INTERVAL * 3
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


class TailResult(BaseModel):
    """Completion report sent from a tail thread to the manager."""

    service: str
    elapsed_seconds: float
    stopped: bool
    failed: bool


class ParsedLogLine(BaseModel):
    """One Docker log event with a stable reconnect identity."""

    text: str
    identity: str
    timestamp: str


DockerClientFactory = Callable[[], docker.DockerClient]
RedisClientFactory = Callable[[], redislib.Redis]


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


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
    containers = client.containers.list(
        filters={"label": f"com.docker.compose.project={COMPOSE_PROJECT}"}
    )

    result: dict[str, docker.models.containers.Container] = {}
    for container in containers:
        service = container.labels.get("com.docker.compose.service")
        if service and service != "log-streamer":
            result[service] = container
    return result


def _recent_identities(
    client: redislib.Redis,
    identity_key: str,
) -> list[str]:
    """Load the bounded deduplication window."""
    try:
        return list(client.lrange(identity_key, 0, BACKFILL_LINES - 1))
    except redislib.RedisError:
        logger.warning("Redis identity lookup failed for %s", identity_key)
        return []


def _migration_state(
    client: redislib.Redis,
    buffer_key: str,
    identity_key: str,
    migration_key: str,
) -> tuple[float | None, bool]:
    try:
        pending_cutoff = client.get(migration_key)
        if pending_cutoff is not None:
            return float(pending_cutoff), False
        if client.lrange(identity_key, 0, 0) or not client.lrange(buffer_key, 0, 0):
            return None, False
        cutoff = time.time()
        started = bool(client.set(migration_key, str(cutoff), nx=True))
        if started:
            return cutoff, True
        persisted_cutoff = client.get(migration_key)
        return (
            float(persisted_cutoff) if persisted_cutoff is not None else cutoff,
            False,
        )
    except (redislib.RedisError, ValueError):
        logger.warning("Redis identity migration state failed for %s", identity_key)
        return None, False


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
                timestamp=timestamp,
            )
        )
    return parsed


def _timestamp_seconds(timestamp: str) -> float | None:
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _store_identity(
    client: redislib.Redis,
    identity_key: str,
    identity: str,
) -> None:
    pipe = client.pipeline(transaction=False)
    pipe.lpush(identity_key, identity)
    pipe.ltrim(identity_key, 0, BUFFER_MAX - 1)
    pipe.execute()


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


def set_health_status(
    healthy: bool,
    redis_factory: RedisClientFactory = _redis_client,
) -> None:
    """Publish manager health independently of log volume."""
    redis_client = redis_factory()
    try:
        redis_client.set(
            HEALTH_STATUS_KEY,
            "ok" if healthy else "failed",
            ex=HEALTH_STATUS_TTL_SECONDS,
        )
    except redislib.RedisError:
        logger.warning("Redis health status update failed")
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
    migration_key = f"{IDENTITY_MIGRATION_PREFIX}{service}"
    logger.info("Tailing %s (container %s)", service, container.short_id)

    started = time.monotonic()
    redis_client = redis_factory()
    recent_identities = _recent_identities(
        redis_client,
        identity_key,
    )
    known_identity_order = deque(
        reversed(recent_identities),
        maxlen=BACKFILL_LINES,
    )
    known_identities = set(recent_identities)
    migration_cutoff, migration_started = _migration_state(
        redis_client,
        buffer_key,
        identity_key,
        migration_key,
    )

    def remember_identity(identity: str) -> None:
        if identity in known_identities:
            return
        if len(known_identity_order) == BACKFILL_LINES:
            known_identities.discard(known_identity_order.popleft())
        known_identity_order.append(identity)
        known_identities.add(identity)

    if migration_started:
        logger.info(
            "Skipping one backfill for %s because the pre-upgrade buffer has no identities",
            service,
        )

    def publish_new(chunk: bytes | str) -> None:
        nonlocal migration_cutoff
        for log_line in _parse_log_chunk(chunk, container.id):
            if log_line.identity in known_identities:
                continue
            if migration_cutoff is not None:
                event_time = _timestamp_seconds(log_line.timestamp)
                if event_time is None or event_time <= migration_cutoff:
                    try:
                        _store_identity(redis_client, identity_key, log_line.identity)
                        remember_identity(log_line.identity)
                    except redislib.RedisError:
                        logger.warning(
                            "Redis identity migration failed for %s",
                            service,
                        )
                    continue
                try:
                    redis_client.delete(migration_key)
                    migration_cutoff = None
                except redislib.RedisError:
                    logger.warning("Redis identity migration completion failed for %s", service)
            try:
                _publish_line(
                    redis_client,
                    channel,
                    buffer_key,
                    identity_key,
                    service,
                    log_line,
                )
                remember_identity(log_line.identity)
            except redislib.RedisError:
                logger.warning("Redis publish failed for %s, will retry", service)

    failed = False
    try:
        for chunk in container.logs(
            stream=True,
            follow=True,
            tail=BACKFILL_LINES,
            timestamps=True,
        ):
            if _stop.is_set():
                break
            publish_new(chunk)
    except Exception:
        failed = True
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
                failed=failed,
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
        self.active_started: dict[str, float] = {}
        self.tail_results: queue.Queue[TailResult] = queue.Queue()
        self.quick_eof_counts: dict[str, int] = {}
        self.recovering_services: set[str] = set()
        self.attachment_failures: set[str] = set()

    def _replace_docker_client(self) -> bool:
        """Replace the Docker SDK client after repeated short-lived streams."""
        try:
            replacement = self._client_factory()
        except Exception:
            logger.exception("Failed to recreate Docker client")
            return False
        previous = self.docker_client
        self.docker_client = replacement
        previous.close()
        logger.warning("Recreated Docker client after repeated quick log stream EOFs")
        return True

    def _process_tail_results(self) -> None:
        """Consume thread results and heal the client after repeated quick EOFs."""
        while True:
            try:
                result = self.tail_results.get_nowait()
            except queue.Empty:
                break
            if result.stopped:
                continue
            if result.failed:
                self.attachment_failures.add(result.service)
            if result.elapsed_seconds < QUICK_EOF_THRESHOLD_SECONDS:
                self.quick_eof_counts[result.service] = (
                    self.quick_eof_counts.get(result.service, 0) + 1
                )
            else:
                self.quick_eof_counts.pop(result.service, None)

        triggered_services = {
            service
            for service, count in self.quick_eof_counts.items()
            if count >= QUICK_EOF_LIMIT
        }
        if triggered_services:
            self.recovering_services.update(triggered_services)
            for service in triggered_services:
                self.quick_eof_counts[service] = 0
            self._replace_docker_client()

    def run_cycle(self) -> None:
        """Run one discovery and reconciliation cycle."""
        refresh_health(self._redis_factory)
        self._process_tail_results()
        try:
            services = discover_services(self.docker_client)
        except Exception:
            logger.exception("Failed to list containers")
            set_health_status(False, self._redis_factory)
            return

        for service in list(self.active):
            if service not in services:
                logger.info("Service %s gone, stopping tail", service)
                del self.active[service]
                self.active_started.pop(service, None)
                self.attachment_failures.discard(service)
                self.recovering_services.discard(service)
                self.quick_eof_counts.pop(service, None)

        for service, container in services.items():
            if service in self.active:
                thread, old_id = self.active[service]
                if container.id != old_id:
                    logger.info("Service %s restarted, restarting tail", service)
                    del self.active[service]
                    self.active_started.pop(service, None)
                elif not thread.is_alive():
                    logger.debug("Tail thread for %s died, respawning", service)
                    del self.active[service]
                    self.active_started.pop(service, None)

        now = time.monotonic()
        stable_services = {
            service
            for service, (thread, _container_id) in self.active.items()
            if thread.is_alive()
            and now - self.active_started.get(service, now)
            >= QUICK_EOF_THRESHOLD_SECONDS
        }
        self.attachment_failures.difference_update(stable_services)
        self.recovering_services.difference_update(stable_services)

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
            self.active_started[service] = time.monotonic()

        set_health_status(
            not self.recovering_services and not self.attachment_failures,
            self._redis_factory,
        )

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
        streamer.run_cycle()
        _stop.wait(DISCOVERY_INTERVAL)

    logger.info("Shutting down, waiting for tail threads")
    streamer.close()
    logger.info("Log-streamer stopped")


if __name__ == "__main__":
    main()
