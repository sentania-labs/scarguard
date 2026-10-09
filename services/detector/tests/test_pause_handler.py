from unittest.mock import MagicMock

import pytest
from atomic_ref import AtomicRef
from pause_handler import PauseHandler
from pause_protocol import MAX_PAUSE_TIMEOUT


@pytest.mark.parametrize(
    "timeout",
    [None, "bad", 0, -1, float("nan"), float("inf"), MAX_PAUSE_TIMEOUT + 1],
)
def test_pause_handler_rejects_invalid_timeout(timeout: object) -> None:
    handler = PauseHandler({}, MagicMock(), AtomicRef(False), MagicMock())
    handler._do_pause = MagicMock()  # type: ignore[method-assign]

    handler._handle_command("test", {"action": "pause", "timeout": timeout})

    handler._do_pause.assert_not_called()


def test_pause_handler_accepts_bounded_timeout() -> None:
    handler = PauseHandler({}, MagicMock(), AtomicRef(False), MagicMock())
    handler._do_pause = MagicMock()  # type: ignore[method-assign]

    handler._handle_command(
        "test",
        {"action": "pause", "request_id": "request-1", "timeout": MAX_PAUSE_TIMEOUT},
    )

    handler._do_pause.assert_called_once_with("request-1", float(MAX_PAUSE_TIMEOUT))
