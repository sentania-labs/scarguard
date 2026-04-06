"""Redis pub/sub publisher for detection events."""

import json
import logging
from collections import deque

import redis as redis_lib

logger = logging.getLogger(__name__)

CHANNEL = "scarguard:detections"

# Maximum events to buffer during Redis outages.  Real-time detection
# events have diminishing value when stale, so a modest bound is fine.
_BUFFER_MAX = 256


class RedisPublisher:
    def __init__(self, host: str, port: int, password: str | None = None) -> None:
        self._client = redis_lib.Redis(host=host, port=port, password=password, decode_responses=True)
        self._buffer: deque[str] = deque(maxlen=_BUFFER_MAX)

    def publish(self, event: dict) -> None:
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
