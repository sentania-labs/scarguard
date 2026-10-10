import json
import threading
from typing import Any
from unittest.mock import patch

from main import subscribe_loop


def test_subscribe_loop_survives_malformed_messages() -> None:
    shutdown_event = threading.Event()
    received_events = []

    class FakeQueue:
        def put_nowait(self, event: dict[str, Any]) -> None:
            if event.get("crash"):
                raise ValueError("Intentional crash")
            received_events.append(event)
            if event.get("class_name") == "valid":
                shutdown_event.set()

    malformed = [
        {"type": "message", "channel": "scarguard:detections", "data": "not-json"},
        {"type": "message", "channel": "scarguard:detections", "data": json.dumps("a string")},
        {"type": "message", "channel": "scarguard:detections", "data": json.dumps(["a list"])},
        {"type": "message", "channel": "scarguard:detections", "data": json.dumps(None)},
        {"type": "message", "channel": "scarguard:detections", "data": json.dumps({"crash": True})},
        {
            "type": "message",
            "channel": "scarguard:detections",
            "data": json.dumps({"class_name": "valid"}),
        },
    ]

    class FakePubSub:
        def __init__(self) -> None:
            self.messages = iter(malformed)

        def subscribe(self, *args: Any) -> None:
            pass

        def get_message(self, timeout: float = 0.0) -> dict[str, Any] | None:
            return next(self.messages, None)

        def unsubscribe(self) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeRedis:
        def pubsub(self) -> FakePubSub:
            return FakePubSub()

        def close(self) -> None:
            pass

    with (
        patch("main.redis_lib.Redis", return_value=FakeRedis()),
        patch("event_signing.load_key_from_env", return_value=None),
    ):
        subscribe_loop({}, FakeQueue(), shutdown_event)

    # verify it kept processing and saw the valid one
    assert len(received_events) == 1
    assert received_events[0]["class_name"] == "valid"
