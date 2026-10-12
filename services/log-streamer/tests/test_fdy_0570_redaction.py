"""FDY-0570 (SG-18): container log lines are masked before Redis publish/buffer."""

from __future__ import annotations

import uuid

import main


def test_parsed_lines_are_redacted_before_publish_or_buffer() -> None:
    secret = f"sentinel{uuid.uuid4().hex}"
    chunk = (
        f"2026-10-10T00:00:00.000000000Z Failed to open RTSP stream: "
        f"rtsp://viewer:{secret}@192.0.2.10/s1\n"
        f"2026-10-10T00:00:01.000000000Z POST https://ntfy.example.com/pond?token={secret}\n"
    ).encode()

    lines = main._parse_log_chunk(chunk, "abc123")

    assert [line.text for line in lines] == [
        "Failed to open RTSP stream: rtsp://192.0.2.10",
        "POST https://ntfy.example.com/pond?token=***",
    ]
    assert all(secret not in line.identity for line in lines)
