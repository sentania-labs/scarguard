import json
from unittest.mock import patch

from pause_protocol import PauseClient


def test_wait_for_state_skips_malformed_values_then_accepts_valid_state() -> None:
    request_id = "request-1"
    values = iter(
        [
            "null",
            json.dumps(["paused"]),
            json.dumps("paused"),
            "not-json",
            json.dumps({"state": "paused", "request_id": request_id}),
        ]
    )

    class FakeClient:
        def get(self, _key: str) -> str:
            return next(values)

    client = PauseClient({})
    with (
        patch("pause_protocol.time.monotonic", return_value=0.0),
        patch("pause_protocol.time.sleep"),
    ):
        assert client._wait_for_state(FakeClient(), "paused", request_id, 30.0)
