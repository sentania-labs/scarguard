"""FDY-0570 webhook failures must not disclose capability URLs."""

from __future__ import annotations

import uuid

import pytest
import requests
import webhook
from webhook import WebhookNotifier


def test_webhook_request_error_does_not_retain_url(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = f"sentinel{uuid.uuid4().hex}"
    monkeypatch.setattr(webhook, "validate_external_url", lambda *_args, **_kwargs: None)
    notifier = WebhookNotifier(
        {
            "name": "hook",
            "url": f"https://hooks.example.com/{sentinel}",
            "allow_internal": False,
        },
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise requests.RequestException(f"request failed for /{sentinel}")

    monkeypatch.setattr(requests, "request", fail)
    with pytest.raises(RuntimeError) as caught:
        notifier.send({"timestamp": "", "snapshot_path": None})

    assert sentinel not in str(caught.value)
