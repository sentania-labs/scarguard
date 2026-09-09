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
from typing import Callable, Literal

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
    container_id: str
    generation: int
    elapsed_seconds: float
    stopped: bool
    failed: bool
    stream_attempted: bool


class ParsedLogLine(BaseModel):
    """One Docker log event with a stable reconnect identity."""

    text: str
    identity: str
    timestamp: str


class BufferedLogEntry(BaseModel):
    """One persisted log line and its reconnect identity."""

    v: Literal[1] = 1
    text: str
    identity: str


class BufferState(BaseModel):
    """Decoded Redis buffer state used for reconnect reconciliation."""

    entries: list[BufferedLogEntry]
    recent_identities: list[str]
    has_legacy_entries: bool


DockerClientFactory = Callable[[], docker.DockerClient]
RedisClientFactory = Callable[[], redislib.Redis]
_LEGACY_IDENTITY_PREFIX = "legacy-buffer:"


class TailGeneration:
    """Serialize service writes and reject work from replaced tails."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current = 0

    def advance(self) -> int:
        with self._lock:
            self._current += 1
            return self._current

    def invalidate(self, generation: int) -> None:
        with self._lock:
            if generation == self._current:
                self._current += 1

    def is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._current

    def run_if_current(
        self,
        generation: int,
        action: Callable[[], None],
    ) -> bool:
        with self._lock:
            if generation != self._current:
                return False
            action()
            return True


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


def _buffer_state(
    client: redislib.Redis,
    buffer_key: str,
) -> BufferState | None:
    """Load identities and detect entries written before the coupled schema."""
    try:
        raw_entries = client.lrange(buffer_key, 0, BUFFER_MAX - 1)
    except redislib.RedisError:
        return None

    entries: list[BufferedLogEntry] = []
    identities: list[str] = []
    has_legacy_entries = False
    for index, raw_entry in enumerate(raw_entries):
        try:
            entry = BufferedLogEntry.model_validate_json(raw_entry)
        except ValueError:
            has_legacy_entries = True
            entry = BufferedLogEntry(
                text=raw_entry,
                identity=f"{_LEGACY_IDENTITY_PREFIX}{index}",
            )
        else:
            if index < BACKFILL_LINES:
                identities.append(entry.identity)
        entries.append(entry)
    return BufferState(
        entries=entries,
        recent_identities=identities,
        has_legacy_entries=has_legacy_entries,
    )


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


def _replace_buffer(
    client: redislib.Redis,
    buffer_key: str,
    entries: list[BufferedLogEntry],
) -> None:
    pipe = client.pipeline(transaction=True)
    pipe.delete(buffer_key)
    for entry in reversed(entries[:BUFFER_MAX]):
        pipe.lpush(buffer_key, entry.model_dump_json())
    pipe.execute()


def _couple_legacy_entries(
    existing: list[BufferedLogEntry],
    observed: list[BufferedLogEntry],
) -> list[BufferedLogEntry]:
    remaining = list(existing)
    for observed_entry in observed:
        for index in range(len(remaining) - 1, -1, -1):
            candidate = remaining[index]
            if (
                candidate.identity.startswith(_LEGACY_IDENTITY_PREFIX)
                and candidate.text == observed_entry.text
            ):
                remaining.pop(index)
                break
    return [*reversed(observed), *remaining][:BUFFER_MAX]


def _publish_line(
    client: redislib.Redis,
    channel: str,
    buffer_key: str,
    service: str,
    log_line: ParsedLogLine,
) -> None:
    """Publish and buffer one log line while updating the rolling health key."""
    now = time.time()
    health_member = f"{time.time_ns()}:{next(_health_member_sequence)}:{service}"
    pipe = client.pipeline(transaction=False)
    pipe.publish(channel, log_line.text)
    pipe.lpush(
        buffer_key,
        BufferedLogEntry(text=log_line.text, identity=log_line.identity).model_dump_json(),
    )
    pipe.ltrim(buffer_key, 0, BUFFER_MAX - 1)
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
    generation_guard: TailGeneration,
    generation: int,
    redis_factory: RedisClientFactory = _redis_client,
) -> None:
    """Tail one container and publish its logs. Runs in a thread."""
    channel = f"{CHANNEL_PREFIX}{service}"
    buffer_key = f"{BUFFER_PREFIX}{service}"
    logger.info("Tailing %s (container %s)", service, container.short_id)

    redis_client = redis_factory()

    def report_reconciliation_failure() -> None:
        logger.warning("Redis log buffer lookup failed for %s", buffer_key)
        result_callback(
            TailResult(
                service=service,
                container_id=container.id,
                generation=generation,
                elapsed_seconds=0,
                stopped=_stop.is_set(),
                failed=True,
                stream_attempted=False,
            )
        )

    buffer_state = _buffer_state(
        redis_client,
        buffer_key,
    )
    if buffer_state is None:
        generation_guard.run_if_current(
            generation,
            report_reconciliation_failure,
        )
        redis_client.close()
        logger.info("Tail thread exiting for %s", service)
        return
    known_identity_order = deque(
        reversed(buffer_state.recent_identities),
        maxlen=BACKFILL_LINES,
    )
    known_identities = set(buffer_state.recent_identities)
    migration_cutoff = time.time() if buffer_state.has_legacy_entries else None
    migration_entries: list[BufferedLogEntry] = []

    def remember_identity(identity: str) -> None:
        if identity in known_identities:
            return
        if len(known_identity_order) == BACKFILL_LINES:
            known_identities.discard(known_identity_order.popleft())
        known_identity_order.append(identity)
        known_identities.add(identity)

    def complete_migration() -> bool:
        nonlocal migration_cutoff
        if migration_cutoff is None:
            return True

        def migrate_buffer() -> None:
            migrated_entries = _couple_legacy_entries(
                buffer_state.entries,
                migration_entries,
            )
            _replace_buffer(redis_client, buffer_key, migrated_entries)
            logger.info(
                "Backfill skipped once for %s because the pre-upgrade buffer has no identities",
                service,
            )

        try:
            if not generation_guard.run_if_current(generation, migrate_buffer):
                return False
        except redislib.RedisError:
            logger.warning("Redis log buffer migration failed for %s", service)
            return False
        for entry in migration_entries:
            remember_identity(entry.identity)
        migration_cutoff = None
        return True

    def publish_new(chunk: bytes | str) -> bool:
        nonlocal migration_cutoff
        for log_line in _parse_log_chunk(chunk, container.id):
            if not generation_guard.is_current(generation):
                return False
            if log_line.identity in known_identities:
                continue
            if migration_cutoff is not None:
                event_time = _timestamp_seconds(log_line.timestamp)
                if event_time is None or event_time <= migration_cutoff:
                    migration_entries.append(
                        BufferedLogEntry(
                            text=log_line.text,
                            identity=log_line.identity,
                        )
                    )
                    continue
                if not complete_migration():
                    return False
            try:
                published = generation_guard.run_if_current(
                    generation,
                    lambda: _publish_line(
                        redis_client,
                        channel,
                        buffer_key,
                        service,
                        log_line,
                    ),
                )
                if published:
                    remember_identity(log_line.identity)
            except redislib.RedisError:
                logger.warning("Redis publish failed for %s, will retry", service)
        return True

    failed = False
    started = time.monotonic()
    try:
        if not generation_guard.is_current(generation):
            return
        for chunk in container.logs(
            stream=True,
            follow=True,
            tail=BACKFILL_LINES,
            timestamps=True,
        ):
            if _stop.is_set():
                break
            if not publish_new(chunk):
                failed = True
                break
    except Exception:
        failed = True
        if not _stop.is_set():
            generation_guard.run_if_current(
                generation,
                lambda: logger.warning(
                    "Log stream ended for %s",
                    service,
                    exc_info=True,
                ),
            )
    finally:
        elapsed = time.monotonic() - started
        stopped = _stop.is_set()
        if not failed and not stopped and migration_entries:
            failed = not complete_migration()
        redis_client.close()
        generation_guard.run_if_current(
            generation,
            lambda: result_callback(
                TailResult(
                    service=service,
                    container_id=container.id,
                    generation=generation,
                    elapsed_seconds=elapsed,
                    stopped=stopped,
                    failed=failed,
                    stream_attempted=True,
                )
            ),
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
        self.active_generations: dict[str, int] = {}
        self.active_started: dict[str, float] = {}
        self.tail_generations: dict[str, TailGeneration] = {}
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

    def _process_tail_results(
        self,
        services: dict[str, docker.models.containers.Container],
    ) -> bool:
        """Consume thread results and heal the client after repeated quick EOFs."""
        while True:
            try:
                result = self.tail_results.get_nowait()
            except queue.Empty:
                break
            active = self.active.get(result.service)
            discovered = services.get(result.service)
            if (
                active is None
                or discovered is None
                or result.generation != self.active_generations.get(result.service)
                or result.container_id != active[1]
                or result.container_id != discovered.id
            ):
                continue
            if result.stopped:
                continue
            if result.failed:
                self.attachment_failures.add(result.service)
            if not result.stream_attempted:
                continue
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
            return self._replace_docker_client()
        return False

    def run_cycle(self) -> None:
        """Run one discovery and reconciliation cycle."""
        refresh_health(self._redis_factory)
        try:
            services = discover_services(self.docker_client)
        except Exception:
            logger.exception("Failed to list containers")
            set_health_status(False, self._redis_factory)
            return
        if self._process_tail_results(services):
            try:
                services = discover_services(self.docker_client)
            except Exception:
                logger.exception("Failed to list containers after recreating Docker client")
                set_health_status(False, self._redis_factory)
                return

        for service in list(self.active):
            if service not in services:
                logger.info("Service %s gone, stopping tail", service)
                generation = self.active_generations.pop(service)
                self.tail_generations[service].invalidate(generation)
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
                    generation = self.active_generations.pop(service)
                    self.tail_generations[service].invalidate(generation)
                    del self.active[service]
                    self.active_started.pop(service, None)
                elif not thread.is_alive():
                    logger.debug("Tail thread for %s died, respawning", service)
                    del self.active[service]
                    self.active_generations.pop(service)
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
            generation_guard = self.tail_generations.setdefault(
                service,
                TailGeneration(),
            )
            generation = generation_guard.advance()
            thread = threading.Thread(
                target=tail_container,
                args=(
                    service,
                    container,
                    self.tail_results.put,
                    generation_guard,
                    generation,
                    self._redis_factory,
                ),
                name=f"tail-{service}",
                daemon=True,
            )
            thread.start()
            self.active[service] = (thread, container.id)
            self.active_generations[service] = generation
            self.active_started[service] = time.monotonic()

        set_health_status(
            not self.recovering_services and not self.attachment_failures,
            self._redis_factory,
        )

    def close(self) -> None:
        """Close manager resources during process shutdown."""
        for service, generation in self.active_generations.items():
            self.tail_generations[service].invalidate(generation)
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
