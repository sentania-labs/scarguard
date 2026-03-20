"""ScarGuard notifier — subscribes to Redis detections and dispatches notifications."""

import json
import logging
import os
import signal
import sys
import time

import redis as redis_lib
import yaml

from discord import DiscordNotifier
from email_notifier import EmailNotifier

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


def build_notifiers(notif_cfg: dict) -> list:
    notifiers = []

    discord_cfg = notif_cfg.get("discord", {})
    if discord_cfg.get("enabled") and discord_cfg.get("webhook_url"):
        notifiers.append(DiscordNotifier(discord_cfg))
        logger.info("Discord notifier enabled")

    email_cfg = notif_cfg.get("email", {})
    if email_cfg.get("enabled") and email_cfg.get("smtp_host"):
        notifiers.append(EmailNotifier(email_cfg))
        logger.info("Email notifier enabled")

    return notifiers


def dispatch(event: dict, notifiers: list) -> None:
    for notifier in notifiers:
        try:
            notifier.send(event)
        except Exception:
            logger.exception("Unhandled error in %s", type(notifier).__name__)


def subscribe_loop(redis_cfg: dict, notifiers: list, shutdown_flag: list) -> None:
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
                dispatch(event, notifiers)

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

    notifiers = build_notifiers(cfg.get("notifications", {}))
    if not notifiers:
        logger.warning("No notifiers enabled — will consume events without dispatching")

    # Use a mutable flag so the signal handler can stop the blocking listen loop.
    shutdown_flag = [False]

    def _shutdown(sig: int, _frame: object) -> None:
        logger.info("Received signal %s — shutting down", sig)
        shutdown_flag[0] = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    subscribe_loop(cfg.get("redis", {}), notifiers, shutdown_flag)
    logger.info("Notifier stopped cleanly")


if __name__ == "__main__":
    main()
