"""Redis pub/sub publisher for detection events."""

import json
import logging

import redis as redis_lib

logger = logging.getLogger(__name__)

CHANNEL = "scarguard:detections"


class RedisPublisher:
    def __init__(self, host: str, port: int) -> None:
        self._client = redis_lib.Redis(host=host, port=port, decode_responses=True)

    def publish(self, event: dict) -> None:
        try:
            self._client.publish(CHANNEL, json.dumps(event, default=str))
            logger.debug("Published %s event to %s", event.get("class_name"), CHANNEL)
        except redis_lib.RedisError:
            logger.exception("Failed to publish event to Redis")
