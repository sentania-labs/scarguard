"""ScarGuard notifier — subscribes to Redis detections and dispatches notifications."""

import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Optional

import redis as redis_lib
import yaml
from config_watcher import ConfigWatcher
from discord import DiscordNotifier
from email_notifier import EmailNotifier
from notification_queue import WORKER_INTERVAL, NotificationQueue

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/scarguard.yml")
CHANNEL = "scarguard:detections"

# How long to wait before retrying a failed Redis connection (seconds).
_REDIS_RECONNECT_DELAY = 5
_REDIS_MAX_RECONNECT_DELAY = 60


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def setup_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,
    )


def build_notifiers(notif_cfg: dict, tz_name: str = "UTC") -> list:
    notifiers: list[DiscordNotifier | EmailNotifier] = []

    discord_cfg = notif_cfg.get("discord", {})
    if discord_cfg.get("enabled") and discord_cfg.get("webhook_url"):
        notifiers.append(DiscordNotifier(discord_cfg, tz_name))
        logger.info("Discord notifier enabled")

    email_cfg = notif_cfg.get("email", {})
    if email_cfg.get("enabled") and email_cfg.get("smtp_host"):
        notifiers.append(EmailNotifier(email_cfg, tz_name))
        logger.info("Email notifier enabled")

    return notifiers


def dispatch(
    event: dict,
    notifiers: list,
    notifiers_lock: Optional[threading.Lock] = None,
    queue: Optional[NotificationQueue] = None,
) -> None:
    """Send an event to all active notifiers.

    If a notifier raises (e.g. network error), the event is enqueued for retry
    when a queue is provided.  Without a queue the error is logged and the next
    notifier is still attempted.
    """
    if notifiers_lock is not None:
        with notifiers_lock:
            current = list(notifiers)
    else:
        current = list(notifiers)

    for notifier in current:
        try:
            notifier.send(event)
        except Exception:
            if queue is not None:
                logger.warning(
                    "%s failed — event queued for retry (queue depth: %d)",
                    type(notifier).__name__,
                    queue.depth,
                )
                queue.enqueue(event, notifier)
            else:
                logger.exception("Unhandled error in %s (no retry queue)", type(notifier).__name__)


def _start_retry_worker(
    queue: NotificationQueue,
    notifiers: list,
    notifiers_lock: threading.Lock,
    shutdown_flag: list,
) -> threading.Thread:
    """Start the background thread that processes due retry queue entries."""

    def _worker() -> None:
        logger.info("Notification retry worker started (interval: %ds)", WORKER_INTERVAL)
        while not shutdown_flag[0]:
            try:
                queue.process_due(notifiers, notifiers_lock)
            except Exception:
                logger.exception("Unexpected error in notification retry worker")
            time.sleep(WORKER_INTERVAL)
        logger.info("Notification retry worker stopped")

    t = threading.Thread(target=_worker, name="notif-retry-worker", daemon=True)
    t.start()
    return t


def subscribe_loop(
    redis_cfg: dict,
    notifiers: list,
    notifiers_lock: threading.Lock,
    shutdown_flag: list,
    queue: NotificationQueue,
) -> None:
    """Connect to Redis and listen for events, reconnecting on failure."""
    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))
    delay = _REDIS_RECONNECT_DELAY

    while not shutdown_flag[0]:
        try:
            client = redis_lib.Redis(host=host, port=port, decode_responses=True)
            pubsub = client.pubsub()
            pubsub.subscribe(CHANNEL)
            logger.info("Subscribed to Redis channel: %s", CHANNEL)
            delay = _REDIS_RECONNECT_DELAY  # reset backoff on successful connect

            for message in pubsub.listen():
                if shutdown_flag[0]:
                    break
                if message["type"] != "message":
                    continue
                try:
                    event = json.loads(message["data"])
                except json.JSONDecodeError:
                    logger.warning("Received malformed message: %s", message["data"])
                    continue

                logger.info(
                    "Event received: %s from %s (conf=%.2f)",
                    event.get("class_name"),
                    event.get("camera_name"),
                    event.get("confidence", 0.0),
                )
                dispatch(event, notifiers, notifiers_lock, queue)

        except redis_lib.RedisError:
            if shutdown_flag[0]:
                break
            logger.exception(
                "Redis connection lost — retrying in %ds", delay
            )
            time.sleep(delay)
            delay = min(delay * 2, _REDIS_MAX_RECONNECT_DELAY)

    logger.info("Subscription loop exited")


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.get("system", {}).get("log_level", "info"))
    logger.info("ScarGuard notifier starting")

    tz_name = cfg.get("system", {}).get("timezone", "UTC")
    notifiers = build_notifiers(cfg.get("notifications", {}), tz_name)
    notifiers_lock = threading.Lock()
    if not notifiers:
        logger.warning("No notifiers enabled — will consume events without dispatching")

    queue = NotificationQueue()
    if queue.depth:
        logger.info("Resuming with %d notification(s) pending in retry queue", queue.depth)

    # Use a mutable flag so the signal handler can stop the blocking listen loop.
    shutdown_flag = [False]

    def _shutdown(sig: int, _frame: object) -> None:
        logger.info("Received signal %s — shutting down", sig)
        shutdown_flag[0] = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    def _on_config_change(new_cfg: dict) -> None:
        new_tz = new_cfg.get("system", {}).get("timezone", "UTC")
        new_notifiers = build_notifiers(new_cfg.get("notifications", {}), new_tz)
        with notifiers_lock:
            notifiers.clear()
            notifiers.extend(new_notifiers)
        if new_notifiers:
            logger.info(
                "Config reloaded — notifiers: %s",
                ", ".join(type(n).__name__ for n in new_notifiers),
            )
        else:
            logger.info("Config reloaded — no notifiers enabled")

    watcher = ConfigWatcher(CONFIG_PATH, _on_config_change)
    watcher.start()

    _start_retry_worker(queue, notifiers, notifiers_lock, shutdown_flag)

    subscribe_loop(cfg.get("redis", {}), notifiers, notifiers_lock, shutdown_flag, queue)

    watcher.stop()
    logger.info("Notifier stopped cleanly")


if __name__ == "__main__":
    main()
