import json
import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

from redis_client import reconnect_loop


def test_reconnect_loop_survives_malformed_messages() -> None:
    shutdown = threading.Event()
    received_messages = []

    def handler(channel: str, payload: dict[str, Any]) -> None:
        if payload.get("crash"):
            raise ValueError("Intentional crash")
        received_messages.append(payload)
        if len(received_messages) == 1:
            shutdown.set()

    malformed = [
        {"type": "message", "channel": "test", "data": "not-json"},
        {"type": "message", "channel": "test", "data": json.dumps("a string")},
        {"type": "message", "channel": "test", "data": json.dumps(["a list"])},
        {"type": "message", "channel": "test", "data": json.dumps(None)},
        {"type": "message", "channel": "test", "data": json.dumps({"crash": True})},
        {"type": "message", "channel": "test", "data": json.dumps({"valid": True})},
    ]

    class FakePubSub:
        def subscribe(self, *args: Any) -> None:
            pass

        def listen(self) -> Iterator[dict[str, Any]]:
            for m in malformed:
                yield m

        def unsubscribe(self) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeRedis:
        def pubsub(self) -> FakePubSub:
            return FakePubSub()

        def close(self) -> None:
            pass

    with patch("redis_client.make_sync_client", return_value=FakeRedis()):
        reconnect_loop({}, ["test"], handler, shutdown)

    assert len(received_messages) == 1
    assert received_messages[0] == {"valid": True}
