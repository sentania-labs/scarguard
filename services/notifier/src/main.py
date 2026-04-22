"""ScarGuard notifier — subscribes to Redis detections and dispatches notifications."""

import json
import logging
import os
import pathlib
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import redis as redis_lib
import yaml
from atomic_ref import AtomicRef
from config_watcher import ConfigWatcher
from digest_scheduler import DigestScheduler
from discord import DiscordNotifier
from email_notifier import EmailNotifier
from notification_queue import WORKER_INTERVAL, NotificationQueue
from ntfy import NtfyNotifier
from webhook import WebhookNotifier

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/scarguard.yml")
CHANNEL = "scarguard:detections"
HEALTH_CHANNEL = "scarguard:health"

# How long to wait before retrying a failed Redis connection (seconds).
_REDIS_RECONNECT_DELAY = 5
_REDIS_MAX_RECONNECT_DELAY = 60


def _derive_base_url(cfg: dict) -> str:
    """Derive the external base URL from tls.domain.

    In v0.12.4, system.base_url was removed.  The notifier now constructs the
    URL from tls.domain (which Caddy uses for HTTPS).
    """
    tls = cfg.get("tls", {})
    domain = tls.get("domain", "").strip()
    if not domain:
        return ""
    mode = tls.get("mode", "off")
    if mode in ("auto", "manual"):
        return f"https://{domain}"
    # mode=off with a domain set — user may be behind an external proxy
    return f"https://{domain}"


def _decrypt_secrets(cfg: dict) -> None:
    """Decrypt sensitive channel fields in *cfg* in place if a key is available.
    No-op if the secret key is absent — operator hasn't run setup yet, or
    the deployment is mid-upgrade with plaintext secrets still on disk."""
    import secret_box
    key = secret_box.try_load_key()
    if key is None:
        return
    try:
        secret_box.decrypt_in_place(cfg, key)
    except secret_box.SecretKeyMissing:
        logger.error("Failed to decrypt notifier secrets — wrong key on disk?")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    if isinstance(cfg, dict):
        _decrypt_secrets(cfg)
    return cfg


def setup_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,
    )


def build_notifiers(notif_cfg: dict, tz_name: str = "UTC") -> list[DiscordNotifier | EmailNotifier | WebhookNotifier | NtfyNotifier]:
    """Build the list of active notifiers from ``notifications.channels``."""
    notifiers: list[DiscordNotifier | EmailNotifier | WebhookNotifier | NtfyNotifier] = []

    channels: list[dict] = notif_cfg.get("channels") or []
    seen_names: set[str] = set()
    for ch in channels:
        if not ch.get("enabled", True):
            continue
        ch_type = ch.get("type", "").lower()
        ch_name = ch.get("name", ch_type)
        if ch_name in seen_names:
            logger.warning("Duplicate channel name %r — skipping second definition", ch_name)
            continue
        try:
            if ch_type == "discord" and ch.get("webhook_url"):
                notifiers.append(DiscordNotifier(ch, tz_name))
                logger.info("Discord channel [%s] enabled", ch_name)
                seen_names.add(ch_name)
            elif ch_type == "email" and ch.get("smtp_host"):
                notifiers.append(EmailNotifier(ch, tz_name))
                logger.info("Email channel [%s] enabled", ch_name)
                seen_names.add(ch_name)
            elif ch_type == "webhook" and ch.get("url"):
                notifiers.append(WebhookNotifier(ch, tz_name))
                logger.info("Webhook channel [%s] enabled → %s", ch_name, ch["url"])
                seen_names.add(ch_name)
            elif ch_type == "ntfy" and ch.get("topic"):
                notifiers.append(NtfyNotifier(ch, tz_name))
                logger.info("Ntfy channel [%s] enabled → %s", ch_name, ch.get("server", "https://ntfy.sh"))
                seen_names.add(ch_name)
            elif ch_type:
                logger.warning("Unknown channel type %r for [%s], skipping", ch_type, ch_name)
        except Exception as exc:
            logger.error("Failed to build channel [%s]: %s", ch_name, exc)

    return notifiers


def dispatch(
    event: dict,
    notifiers: list[DiscordNotifier | EmailNotifier | WebhookNotifier | NtfyNotifier],
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

    # actions_triggered semantics:
    #   absent → legacy event or no action rules; notify all channels
    #   None   → notification rules exist but no rule matched; suppress entirely
    #   []     → no notification rules configured; notify all channels
    #   [...]  → notify only the named channels
    _MISSING = object()
    actions_raw = event.get("actions_triggered", _MISSING)
    if actions_raw is _MISSING:
        actions_triggered: list[str] = []
    elif actions_raw is None:
        logger.debug("Event suppressed by notification rules — no notifications")
        return
    else:
        actions_triggered = actions_raw
    if actions_triggered:
        current = [n for n in current if getattr(n, "name", None) in actions_triggered]
        if not current:
            logger.warning(
                "actions_triggered=%s but no matching notifiers found "
                "(check channel names in notification_rules vs notifications.channels)",
                actions_triggered,
            )

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
    shutdown_event: threading.Event,
) -> threading.Thread:
    """Start the background thread that processes due retry queue entries."""

    def _worker() -> None:
        logger.info("Notification retry worker started (interval: %ds)", WORKER_INTERVAL)
        while not shutdown_event.is_set():
            try:
                queue.process_due(notifiers, notifiers_lock)
            except Exception:
                logger.exception("Unexpected error in notification retry worker")
            shutdown_event.wait(WORKER_INTERVAL)
        logger.info("Notification retry worker stopped")

    t = threading.Thread(target=_worker, name="notif-retry-worker", daemon=True)
    t.start()
    return t


def subscribe_loop(
    redis_cfg: dict,
    notifiers: list,
    notifiers_lock: threading.Lock,
    shutdown_event: threading.Event,
    queue: NotificationQueue,
    _base_url_ref: AtomicRef[str] | None = None,
) -> None:
    """Connect to Redis and listen for events, reconnecting on failure.

    v1.14 verifies the HMAC signature on detection events before
    dispatching a notification. Unlike the deterrent, a missing or invalid
    signature here only suppresses the notification (no physical effect),
    but we still log loudly — spoofed events would otherwise leak camera
    snapshots to the attacker's own webhook destinations.
    """
    from event_signing import load_key_from_env, verify_event

    host = redis_cfg.get("host", "redis")
    port = int(redis_cfg.get("port", 6379))
    hmac_key = load_key_from_env()
    if hmac_key is None:
        logger.warning(
            "DETECTION_HMAC_KEY not set — dispatching unsigned events.",
        )
    unsigned_warned = False
    invalid_warned = False
    delay = _REDIS_RECONNECT_DELAY

    while not shutdown_event.is_set():
        client: redis_lib.Redis | None = None
        pubsub: redis_lib.client.PubSub | None = None
        try:
            redis_password = os.environ.get("REDIS_PASSWORD", "") or None
            client = redis_lib.Redis(host=host, port=port, password=redis_password, decode_responses=True)
            pubsub = client.pubsub()
            pubsub.subscribe(CHANNEL, HEALTH_CHANNEL)
            logger.info("Subscribed to Redis channels: %s, %s", CHANNEL, HEALTH_CHANNEL)
            delay = _REDIS_RECONNECT_DELAY  # reset backoff on successful connect
            pathlib.Path("/tmp/healthy").touch(exist_ok=True)

            for message in pubsub.listen():
                if shutdown_event.is_set():
                    break
                if message["type"] != "message":
                    continue
                # Touch health marker so Docker health check knows we're alive
                pathlib.Path("/tmp/healthy").touch(exist_ok=True)
                try:
                    event = json.loads(message["data"])
                except json.JSONDecodeError:
                    logger.warning("Received malformed message: %s", message["data"])
                    continue

                # Signature verification (detection channel only — health alerts
                # come from the detector's health publisher, not the detection
                # publisher, and aren't signed today).
                if message["channel"] == CHANNEL and hmac_key is not None:
                    if not verify_event(event, hmac_key):
                        if not invalid_warned:
                            logger.error(
                                "Rejecting detection event with invalid/missing "
                                "HMAC signature — NOT notifying. Camera=%s class=%s. "
                                "Further invalid events at DEBUG.",
                                event.get("camera_name"),
                                event.get("class_name"),
                            )
                            invalid_warned = True
                        else:
                            logger.debug("Invalid-signature event rejected")
                        continue
                elif message["channel"] == CHANNEL and hmac_key is None and not unsigned_warned:
                    unsigned_warned = True
                    logger.warning(
                        "Accepting unsigned detection event. Further unsigned events at DEBUG.",
                    )

                # Health alerts get formatted as notification events
                if message["channel"] == HEALTH_CHANNEL:
                    alert_event = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "class_name": "camera_offline",
                        "confidence": 1.0,
                        "camera_name": event.get("camera_name", "unknown"),
                        "snapshot_path": None,
                    }
                    logger.warning(
                        "Camera health alert: %s offline for %ss",
                        event.get("camera_name"),
                        event.get("offline_seconds"),
                    )
                    dispatch(alert_event, notifiers, notifiers_lock, queue)
                    continue

                # Inject base_url so notifiers can build feedback links
                base_url = _base_url_ref.get() if _base_url_ref else ""
                if base_url:
                    event["_base_url"] = base_url

                logger.info(
                    "Event received: %s from %s (conf=%.2f)",
                    event.get("class_name"),
                    event.get("camera_name"),
                    event.get("confidence", 0.0),
                )
                dispatch(event, notifiers, notifiers_lock, queue)

        except redis_lib.RedisError:
            if shutdown_event.is_set():
                break
            logger.exception(
                "Redis connection lost — retrying in %ds", delay
            )
            time.sleep(delay)
            delay = min(delay * 2, _REDIS_MAX_RECONNECT_DELAY)
        finally:
            if pubsub is not None:
                try:
                    pubsub.unsubscribe()
                    pubsub.close()
                except Exception:
                    pass
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    logger.info("Subscription loop exited")


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.get("system", {}).get("log_level", "info"))
    logger.info("ScarGuard notifier starting")

    tz_name = cfg.get("system", {}).get("timezone", "UTC")
    notifiers = build_notifiers(cfg.get("notifications", {}), tz_name)
    notifiers_lock = threading.Lock()
    base_url_ref: AtomicRef[str] = AtomicRef(_derive_base_url(cfg))
    if not notifiers:
        logger.warning("No notifiers enabled — will consume events without dispatching")

    queue = NotificationQueue()
    if queue.depth:
        logger.info("Resuming with %d notification(s) pending in retry queue", queue.depth)

    # ---- Digest scheduler --------------------------------------------------------
    digest_scheduler = DigestScheduler(
        dispatch_fn=dispatch,
        notifiers=notifiers,
        notifiers_lock=notifiers_lock,
    )
    report_cfg = cfg.get("system", {}).get("summary_report", {})
    digest_scheduler.configure(report_cfg, tz_name)
    digest_scheduler.start()

    # Use a threading.Event so the signal handler can stop the blocking listen loop.
    shutdown_event = threading.Event()

    def _shutdown(sig: int, _frame: object) -> None:
        logger.info("Received signal %s — shutting down", sig)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    def _on_config_change(new_cfg: dict) -> None:
        # Decrypt sensitive fields before any consumer sees the dict.
        # ConfigWatcher reads raw YAML; we own the decrypt step at the
        # service boundary.
        _decrypt_secrets(new_cfg)
        new_tz = new_cfg.get("system", {}).get("timezone", "UTC")
        base_url_ref.set(_derive_base_url(new_cfg))
        new_notifiers = build_notifiers(new_cfg.get("notifications", {}), new_tz)
        with notifiers_lock:
            notifiers.clear()
            notifiers.extend(new_notifiers)
        if new_notifiers:
            logger.info(
                "Config reloaded — notifiers: %s",
                ", ".join(getattr(n, "name", type(n).__name__) for n in new_notifiers),
            )
        else:
            logger.info("Config reloaded — no notifiers enabled")

        # Update digest scheduler with new config
        new_report_cfg = new_cfg.get("system", {}).get("summary_report", {})
        digest_scheduler.configure(new_report_cfg, new_tz)

    watcher = ConfigWatcher(CONFIG_PATH, _on_config_change)
    watcher.start()

    _start_retry_worker(queue, notifiers, notifiers_lock, shutdown_event)

    subscribe_loop(cfg.get("redis", {}), notifiers, notifiers_lock, shutdown_event, queue, base_url_ref)

    watcher.stop()
    digest_scheduler.stop()
    logger.info("Notifier stopped cleanly")


if __name__ == "__main__":
    main()
