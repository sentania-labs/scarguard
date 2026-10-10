"""Independent OFF-only watchdog for deterrent activation leases."""

from __future__ import annotations

import logging
import os
import pathlib
import signal
import threading
import time
from typing import Any

import redis
import secret_box
import yaml
from activation_lease import (
    LEASE_KEY_ENV,
    LEASE_KEY_PREFIX,
    RedisActivationLeases,
    delete_if_unchanged,
    lease_ttl_sec,
    parse_signed_lease,
)
from deterrent_safety import MAX_ACTUATION_SEC
from event_signing import load_key_from_env
from off_controller import OffOnlyCloudController
from pydantic import BaseModel

logger = logging.getLogger(__name__)
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/scarguard.yml")
HEALTH_PATH = pathlib.Path("/tmp/healthy")
POLL_INTERVAL_SEC = 1.0

DEFAULT_DP_CODES = {
    "sprinkler": "switch_1",
    "light": "switch_led",
    "sound": "switch",
    "plug": "switch_1",
}


class WatchdogDevice(BaseModel):
    name: str
    device_id: str
    type: str
    enabled: bool = True
    dp_code: str | None = None


def load_runtime() -> tuple[
    dict[str, WatchdogDevice], OffOnlyCloudController | None, dict[str, Any]
]:
    """Load the single ScarGuard config and its encrypted Tuya fields."""
    with open(CONFIG_PATH) as handle:
        cfg = yaml.safe_load(handle) or {}
    key = secret_box.try_load_key()
    if key is not None:
        secret_box.decrypt_in_place(cfg, key)
    raw = cfg.get("deterrent", {})
    devices = {
        device.device_id: device
        for device in (WatchdogDevice.model_validate(item) for item in raw.get("devices", []))
    }
    creds = raw.get("tuya")
    controller = None
    if isinstance(creds, dict) and creds.get("api_key") and creds.get("api_secret"):
        controller = OffOnlyCloudController(
            creds["api_key"],
            creds["api_secret"],
            creds.get("api_region", "us"),
        )
    return devices, controller, cfg.get("redis", {})


def dp_code(device: WatchdogDevice) -> str:
    return device.dp_code or DEFAULT_DP_CODES.get(device.type, "switch_1")


def startup_off_sweep(
    devices: dict[str, WatchdogDevice],
    controller: OffOnlyCloudController,
) -> bool:
    """Conservatively OFF every configured device before accepting health."""
    safe = True
    for device in devices.values():
        if not controller.force_off(device.device_id, dp_code(device)):
            logger.error("Startup OFF was not acknowledged for %s", device.name)
            safe = False
    return safe


def process_expired_leases(
    client: Any,
    devices: dict[str, WatchdogDevice],
    controller: OffOnlyCloudController,
    signing_key: bytes,
    *,
    now: float | None = None,
    monotonic_now: float | None = None,
    observed_deadlines: dict[str, float] | None = None,
) -> tuple[int, bool]:
    """OFF configured devices whose authentic finite leases have expired."""
    current = time.time() if now is None else now
    current_monotonic = time.monotonic() if monotonic_now is None else monotonic_now
    observations = observed_deadlines if observed_deadlines is not None else {}
    handled = 0
    safe = True
    seen_nonces: set[str] = set()
    for key in client.scan_iter(match=f"{LEASE_KEY_PREFIX}*"):
        if str(key).endswith(":deadline"):
            continue
        raw = client.get(key)
        lease = parse_signed_lease(raw, signing_key)
        if lease is None:
            logger.warning("Ignoring malformed or unauthenticated activation lease")
            continue
        seen_nonces.add(lease.nonce)
        device = devices.get(lease.device_id)
        if device is None:
            continue
        remaining_span = lease.expires_at - lease.issued_at
        monotonic_deadline = observations.setdefault(
            lease.nonce,
            current_monotonic + remaining_span,
        )
        deadline_exists = bool(
            client.exists(RedisActivationLeases.deadline_key(lease.device_id)),
        )
        if (
            deadline_exists
            and lease.expires_at > current
            and monotonic_deadline > current_monotonic
        ):
            continue
        if controller.force_off(device.device_id, dp_code(device)):
            if raw is not None:
                delete_if_unchanged(
                    client,
                    key,
                    raw,
                    deadline_key=RedisActivationLeases.deadline_key(lease.device_id),
                )
            observations.pop(lease.nonce, None)
            handled += 1
        else:
            logger.error("Expired-lease OFF was not acknowledged for %s", device.name)
            safe = False
    for nonce in set(observations) - seen_nonces:
        observations.pop(nonce, None)
    return handled, safe


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    signing_key = load_key_from_env(LEASE_KEY_ENV)
    if signing_key is None:
        raise RuntimeError("OFF watchdog signing key is required")
    devices, controller, redis_cfg = load_runtime()
    if controller is None:
        raise RuntimeError("Tuya credentials are required for OFF watchdog")
    client = redis.Redis(
        host=redis_cfg.get("host", "redis"),
        port=int(redis_cfg.get("port", 6379)),
        password=os.environ.get("REDIS_PASSWORD", "") or None,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    shutdown = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: shutdown.set())
    signal.signal(signal.SIGINT, lambda *_: shutdown.set())
    startup_safe = startup_off_sweep(devices, controller)
    config_mtime = pathlib.Path(CONFIG_PATH).stat().st_mtime_ns
    observed_deadlines: dict[str, float] = {}
    retired_routes: list[tuple[dict[str, WatchdogDevice], OffOnlyCloudController, float]] = []
    while not shutdown.is_set():
        try:
            if not startup_safe:
                startup_safe = startup_off_sweep(devices, controller)
            current_mtime = pathlib.Path(CONFIG_PATH).stat().st_mtime_ns
            if current_mtime != config_mtime:
                try:
                    new_devices, new_controller, _ = load_runtime()
                    if new_controller is None:
                        raise RuntimeError("reloaded config has no Tuya credentials")
                    # Prove the old credential/device route safe before it is
                    # discarded, then prove the replacement safe before use.
                    if not startup_off_sweep(devices, controller):
                        raise RuntimeError("old device registry OFF sweep failed")
                    if not startup_off_sweep(new_devices, new_controller):
                        raise RuntimeError("new device registry OFF sweep failed")
                    retired_routes.append(
                        (
                            devices,
                            controller,
                            time.monotonic() + lease_ttl_sec(MAX_ACTUATION_SEC) + 1.0,
                        ),
                    )
                    devices, controller = new_devices, new_controller
                    startup_safe = True
                    config_mtime = current_mtime
                    logger.info("Reloaded watchdog device registry and credentials")
                except Exception:
                    logger.exception(
                        "Watchdog config reload failed; retaining last safe config",
                    )
            _, leases_safe = process_expired_leases(
                client,
                devices,
                controller,
                signing_key,
                observed_deadlines=observed_deadlines,
            )
            retained: list[tuple[dict[str, WatchdogDevice], OffOnlyCloudController, float]] = []
            for old_devices, old_controller, retire_at in retired_routes:
                _, old_safe = process_expired_leases(
                    client,
                    old_devices,
                    old_controller,
                    signing_key,
                    observed_deadlines=observed_deadlines,
                )
                leases_safe = leases_safe and old_safe
                if time.monotonic() < retire_at:
                    retained.append((old_devices, old_controller, retire_at))
            retired_routes = retained
            if startup_safe and leases_safe:
                HEALTH_PATH.touch(exist_ok=True)
            else:
                HEALTH_PATH.unlink(missing_ok=True)
        except redis.RedisError:
            logger.exception("Redis unavailable; retrying")
        shutdown.wait(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
