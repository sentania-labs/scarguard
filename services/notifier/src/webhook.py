"""Webhook dispatcher — sends detection events as HTTP POST/PUT to a configured URL."""

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

logger = logging.getLogger(__name__)


def _to_local(iso_str: str, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError, TypeError):
        tz = ZoneInfo("UTC")
    try:
        dt = datetime.fromisoformat(iso_str).astimezone(tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, TypeError):
        return str(iso_str)


class WebhookNotifier:
    """Sends a JSON payload to an arbitrary HTTP endpoint on each detection."""

    def __init__(self, cfg: dict, tz_name: str = "UTC") -> None:
        self._name: str = cfg["name"]
        self._url: str = cfg["url"]
        self._method: str = cfg.get("method", "POST").upper()
        self._headers: dict[str, str] = dict(cfg.get("headers") or {})
        self._auth_token: str = cfg.get("auth_token", "")
        self._tz_name: str = tz_name
        if self._auth_token:
            self._headers.setdefault("Authorization", f"Bearer {self._auth_token}")

    @property
    def name(self) -> str:
        return self._name

    def send(self, event: dict) -> None:
        snap = event.get("snapshot_path")
        payload = {
            "timestamp": event.get("timestamp"),
            "camera": event.get("camera_name"),
            "class_name": event.get("class_name"),
            "confidence": event.get("confidence"),
            "snapshot_filename": Path(snap).name if snap else None,
            "display_time": _to_local(str(event.get("timestamp", "")), self._tz_name),
        }

        resp = requests.request(
            self._method,
            self._url,
            json=payload,
            headers=self._headers,
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(
            "Webhook [%s] %s %s → %d",
            self._name, self._method, self._url, resp.status_code,
        )
