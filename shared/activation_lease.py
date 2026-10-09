"""Authenticated, bounded activation leases shared by deterrent and watchdog."""

from __future__ import annotations

import json
import math
import time
import uuid
from typing import Any

from deterrent_safety import CLOUD_CALL_TIMEOUT_SEC, MAX_ACTUATION_SEC, clamp_duration
from event_signing import sign_event, verify_event
from pydantic import BaseModel, Field

LEASE_KEY_PREFIX = "scarguard:off-watchdog:lease:"
LEASE_DEADLINE_SUFFIX = ":deadline"
LEASE_KEY_ENV = "OFF_WATCHDOG_HMAC_KEY"


class ActivationLease(BaseModel):
    """One device that may be ON until a finite wall-clock deadline."""

    version: int = 1
    device_id: str = Field(min_length=1)
    issued_at: float
    expires_at: float
    nonce: str = Field(min_length=1)


def lease_ttl_sec(duration_sec: float) -> float:
    """Return a finite lease covering ON admission plus the clamped duration."""
    duration = clamp_duration(
        duration_sec, max_sec=MAX_ACTUATION_SEC, default=MAX_ACTUATION_SEC,
    )
    return min(duration + CLOUD_CALL_TIMEOUT_SEC, MAX_ACTUATION_SEC + CLOUD_CALL_TIMEOUT_SEC)


class RedisActivationLeases:
    """Publish and clear signed leases; inability to publish denies activation."""

    def __init__(self, client: Any, signing_key: bytes) -> None:
        if len(signing_key) < 16:
            raise ValueError("OFF watchdog signing key must be at least 16 bytes")
        self._client = client
        self._key = signing_key

    @staticmethod
    def redis_key(device_id: str) -> str:
        return f"{LEASE_KEY_PREFIX}{device_id}"

    @classmethod
    def deadline_key(cls, device_id: str) -> str:
        return f"{cls.redis_key(device_id)}{LEASE_DEADLINE_SUFFIX}"

    def arm(self, device_id: str, duration_sec: float) -> ActivationLease:
        now = time.time()
        lease = ActivationLease(
            device_id=device_id,
            issued_at=now,
            expires_at=now + lease_ttl_sec(duration_sec),
            nonce=uuid.uuid4().hex,
        )
        signed = sign_event(lease.model_dump(), self._key)
        deadline_ttl = max(1, math.ceil(lease.expires_at - now))
        pipe = self._client.pipeline(transaction=True)
        # The signed lease intentionally has no Redis TTL. With volatile-lru it
        # cannot be evicted; the small deadline marker may disappear early,
        # which causes a safe early OFF. The watchdog deletes both after OFF.
        pipe.set(self.redis_key(device_id), json.dumps(signed))
        pipe.set(self.deadline_key(device_id), lease.nonce, ex=deadline_ttl)
        results = pipe.execute()
        if results != [True, True]:
            raise ConnectionError("Redis did not acknowledge activation lease")
        return lease

    def clear(self, lease: ActivationLease) -> bool:
        """Clear only the exact lease, never a newer activation's lease."""
        raw = self._client.get(self.redis_key(lease.device_id))
        parsed = parse_signed_lease(raw, self._key)
        if parsed is None or parsed.nonce != lease.nonce:
            return False
        return delete_if_unchanged(
            self._client,
            self.redis_key(lease.device_id),
            raw,
            deadline_key=self.deadline_key(lease.device_id),
        )


def delete_if_unchanged(
    client: Any,
    redis_key: str,
    raw: str | bytes,
    *,
    deadline_key: str | None = None,
) -> bool:
    """Atomically delete *redis_key* only if its exact signed value remains."""
    deadline = deadline_key or f"{redis_key}{LEASE_DEADLINE_SUFFIX}"
    script = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "redis.call('del', KEYS[2]); return redis.call('del', KEYS[1]) "
        "else return 0 end"
    )
    return bool(client.eval(script, 2, redis_key, deadline, raw))


def parse_signed_lease(raw: str | bytes | None, key: bytes) -> ActivationLease | None:
    """Validate a Redis value and return its bounded lease, or reject it."""
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not verify_event(payload, key):
            return None
        lease = ActivationLease.model_validate(payload)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    max_span = MAX_ACTUATION_SEC + CLOUD_CALL_TIMEOUT_SEC
    if lease.expires_at <= lease.issued_at or lease.expires_at - lease.issued_at > max_span:
        return None
    return lease
