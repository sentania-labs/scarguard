import json
import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

from main import subscribe_loop


def test_subscribe_loop_survives_malformed_messages() -> None:
    shutdown_event = threading.Event()
    received_events = []

    def mock_dispatch(
        event: dict[str, Any],
        notifiers: Any,
        notifiers_lock: Any,
        queue: Any,
    ) -> None:
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
        {"type": "message", "channel": "scarguard:detections", "data": json.dumps({"class_name": "valid"})},
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

    with patch("main.redis_lib.Redis", return_value=FakeRedis()), \
         patch("main.dispatch", side_effect=mock_dispatch), \
         patch("event_signing.load_key_from_env", return_value=None):

        subscribe_loop({}, [], MagicMock(), shutdown_event, MagicMock())

    # verify it kept processing and saw the valid one
    assert len(received_events) == 1
    assert received_events[0]["class_name"] == "valid"
