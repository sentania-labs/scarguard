"""Redis pub/sub publisher for detection events."""

import json
import logging
from collections import deque

import redis as redis_lib
from event_signing import load_key_from_env, sign_event

logger = logging.getLogger(__name__)

CHANNEL = "scarguard:detections"

# Maximum events to buffer during Redis outages.  Real-time detection
# events have diminishing value when stale, so a modest bound is fine.
_BUFFER_MAX = 256


class RedisPublisher:
    def __init__(self, host: str, port: int, password: str | None = None) -> None:
        self._client = redis_lib.Redis(host=host, port=port, password=password, decode_responses=True)
        self._buffer: deque[str] = deque(maxlen=_BUFFER_MAX)
        # v1.14: sign every published event so subscribers can authenticate.
        # Key absence is logged loudly at startup; events fall back to
        # unsigned so in-place upgrades don't drop detections.
        self._sign_key: bytes | None = load_key_from_env()
        if self._sign_key is None:
            logger.warning(
                "DETECTION_HMAC_KEY not set — publishing unsigned detection "
                "events. Run setup.sh to generate the key.",
            )
        else:
            logger.info("Detection events will be HMAC-signed")

    def publish(self, event: dict) -> None:
        if self._sign_key is not None:
            event = sign_event(event, self._sign_key)
        payload = json.dumps(event, default=str)
        try:
            self._flush_buffer()
            self._client.publish(CHANNEL, payload)
            logger.debug("Published %s event to %s", event.get("class_name"), CHANNEL)
        except redis_lib.RedisError:
            self._buffer.append(payload)
            logger.warning(
                "Redis publish failed — buffered (%d/%d)",
                len(self._buffer),
                _BUFFER_MAX,
            )

    def _flush_buffer(self) -> None:
        """Re-publish buffered events in FIFO order. Stop on first failure."""
        while self._buffer:
            self._client.publish(CHANNEL, self._buffer[0])
            self._buffer.popleft()
