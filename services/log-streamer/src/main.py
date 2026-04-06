"""ScarGuard log-streamer sidecar.

Tails Docker container logs and publishes them to Redis so the web UI
can stream logs without needing direct Docker socket access.

Each discovered Compose service gets a dedicated tail thread.  Log lines
are published to ``scarguard:logs:{service}`` (pub/sub) and buffered in
``scarguard:logs:buffer:{service}`` (Redis list, newest-first, capped at
BUFFER_MAX) so clients can backfill historical lines on connect.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import threading
from types import FrameType

import docker
import redis as redislib

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("log-streamer")

COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT", "scarguard")
CHANNEL_PREFIX = "scarguard:logs:"
BUFFER_PREFIX = "scarguard:logs:buffer:"
BUFFER_MAX = 2000
DISCOVERY_INTERVAL = 30  # seconds between container discovery sweeps
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mK]")

# Graceful shutdown
_stop = threading.Event()


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _redis_client() -> redislib.Redis:  # type: ignore[type-arg]
    host = os.environ.get("REDIS_HOST", "redis")
    port = int(os.environ.get("REDIS_PORT", "6379"))
    password = os.environ.get("REDIS_PASSWORD", "") or None
    return redislib.Redis(
        host=host, port=port, password=password,
        decode_responses=True, retry_on_timeout=True,
    )


def discover_services(
    client: docker.DockerClient,
) -> dict[str, docker.models.containers.Container]:
    """Return {service_name: container} for running Compose containers."""
    try:
        containers = client.containers.list(
            filters={"label": f"com.docker.compose.project={COMPOSE_PROJECT}"}
        )
    except Exception:
        logger.exception("Failed to list containers")
        return {}

    result: dict[str, docker.models.containers.Container] = {}
    for c in containers:
        service = c.labels.get("com.docker.compose.service")
        # Skip ourselves — tailing our own logs creates a feedback loop
        # when Redis is down (failed-publish warnings get re-tailed).
        if service and service != "log-streamer":
            result[service] = c
    return result


def tail_container(
    service: str,
    container: docker.models.containers.Container,
) -> None:
    """Tail logs from *container* and publish to Redis.  Runs in a thread.

    Creates its own Redis connection so a Redis restart does not require
    restarting the entire sidecar — only affected tail threads reconnect.
    """
    channel = f"{CHANNEL_PREFIX}{service}"
    buffer_key = f"{BUFFER_PREFIX}{service}"
    logger.info("Tailing %s (container %s)", service, container.short_id)

    rc = _redis_client()
    try:
        for chunk in container.logs(stream=True, follow=True, tail=0):
            if _stop.is_set():
                break
            line = (
                chunk.decode("utf-8", errors="replace")
                if isinstance(chunk, bytes)
                else str(chunk)
            )
            clean = _strip_ansi(line).rstrip("\n\r")
            if not clean:
                continue
            try:
                pipe = rc.pipeline(transaction=False)
                pipe.publish(channel, clean)
                pipe.lpush(buffer_key, clean)
                pipe.ltrim(buffer_key, 0, BUFFER_MAX - 1)
                pipe.execute()
            except redislib.RedisError:
                logger.warning("Redis publish failed for %s — will retry", service)
    except Exception:
        if not _stop.is_set():
            logger.warning("Log stream ended for %s", service, exc_info=True)
    finally:
        rc.close()
        logger.info("Tail thread exiting for %s", service)


def main() -> None:
    logger.info("Starting log-streamer sidecar")

    docker_client = docker.from_env()

    # Track active tail threads: service_name -> (thread, container_id)
    active: dict[str, tuple[threading.Thread, str]] = {}

    def _shutdown(signum: int, _frame: FrameType | None) -> None:
        logger.info("Received signal %d — shutting down", signum)
        _stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while not _stop.is_set():
        services = discover_services(docker_client)

        # Stop threads for services that disappeared
        for svc in list(active):
            if svc not in services:
                logger.info("Service %s gone — stopping tail", svc)
                del active[svc]

        # Stop threads where the container ID changed (restarted container)
        for svc, container in services.items():
            if svc in active:
                _, old_id = active[svc]
                if container.id != old_id:
                    logger.info("Service %s restarted — restarting tail", svc)
                    del active[svc]

        # Start threads for new/restarted services
        for svc, container in services.items():
            if svc not in active:
                t = threading.Thread(
                    target=tail_container,
                    args=(svc, container),
                    name=f"tail-{svc}",
                    daemon=True,
                )
                t.start()
                active[svc] = (t, container.id)

        # Clean up threads that have died (container exited)
        for svc in list(active):
            thread, _ = active[svc]
            if not thread.is_alive():
                logger.debug("Tail thread for %s died — will respawn on next cycle", svc)
                del active[svc]

        _stop.wait(DISCOVERY_INTERVAL)

    # Cleanup
    logger.info("Shutting down — waiting for tail threads")
    docker_client.close()
    logger.info("Log-streamer stopped")


if __name__ == "__main__":
    main()
