"""Notification requests from the web UI to the notifier.

The web container is not a detection publisher: its Redis ACL user cannot
PUBLISH on ``scarguard:detections``. "Send test notification" and "share
snapshot" therefore travel on ``scarguard:notify:request``, which only the
notifier subscribes to, so a forged request can never reach the deterrent.
Requests are signed with the channel-derived key when ``DETECTION_HMAC_KEY``
is configured and the notifier verifies them the same way it verifies
detection events.
"""

from __future__ import annotations

import json
from typing import Any

from event_signing import derive_channel_key, load_key_from_env, sign_event

NOTIFY_REQUEST_CHANNEL = "scarguard:notify:request"


def sign_notify_request(event: dict[str, Any], channel: str = NOTIFY_REQUEST_CHANNEL) -> str:
    """Return the JSON payload to publish for *event* on *channel*."""
    key = load_key_from_env()
    if key is None:
        return json.dumps(event)
    return json.dumps(sign_event(event, derive_channel_key(key, channel), channel))
