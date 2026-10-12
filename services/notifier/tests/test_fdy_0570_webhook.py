"""FDY-0570 webhook failures must not disclose capability URLs."""

from __future__ import annotations

import uuid

import pytest
import requests
import webhook
from webhook import WebhookNotifier


def test_webhook_request_error_does_not_retain_url(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = f"sentinel{uuid.uuid4().hex}"
    notifier = WebhookNotifier(
        {
            "name": "hook",
            "url": f"https://hooks.example.com/{sentinel}",
            "allow_internal": False,
        },
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise requests.exceptions.SSLError(f"request failed for /{sentinel}")

    monkeypatch.setattr(webhook.safe_http, "send", fail)
    with pytest.raises(requests.exceptions.SSLError) as caught:
        notifier.send({"timestamp": "", "snapshot_path": None})

    assert sentinel not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
