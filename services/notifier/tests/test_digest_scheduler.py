from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from digest_scheduler import DigestScheduler


@pytest.mark.parametrize("configured_time", [450, "07:30"])
def test_tick_accepts_yaml_numeric_and_string_digest_times(configured_time: object) -> None:
    scheduler = DigestScheduler(MagicMock(), [], MagicMock())
    scheduler.configure(
        {
            "enabled": True,
            "frequency": "daily",
            "time": configured_time,
            "channels": ["email"],
        }
    )
    scheduler._send_digest = MagicMock(return_value=True)  # type: ignore[method-assign]

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: ZoneInfo | None = None) -> "FixedDatetime":
            return cls(2026, 10, 9, 7, 30, tzinfo=tz)

    with patch("digest_scheduler.datetime", FixedDatetime):
        scheduler._tick()

    scheduler._send_digest.assert_called_once_with("daily", ["email"])
