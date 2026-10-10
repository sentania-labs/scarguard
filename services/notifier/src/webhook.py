"""Webhook dispatcher - sends detection events as HTTP POST/PUT to a configured URL."""

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import safe_http
from url_safety import channel_destination_errors

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
        # SSRF defence-in-depth - the web config validator checks this at
        # save, but a hand-edited file bypasses that. Static checks here
        # disable the channel; safe_http re-resolves and pins every send.
        self._allow_internal: bool = cfg.get("allow_internal") is True
        errors = channel_destination_errors({**cfg, "type": "webhook"})
        for err in errors:
            logger.error("Webhook [%s] disabled - %s", self._name, err)
        self._enabled = not errors

    @property
    def name(self) -> str:
        return self._name

    def send(self, event: dict) -> None:
        if not self._enabled:
            logger.warning(
                "Webhook [%s] suppressed - channel disabled at construction",
                self._name,
            )
            return
        if event.get("_digest"):
            self._send_digest(event)
            return

        snap = event.get("snapshot_path")
        payload = {
            "timestamp": event.get("timestamp"),
            "camera": event.get("camera_name"),
            "class_name": event.get("class_name"),
            "confidence": event.get("confidence"),
            "snapshot_filename": Path(snap).name if snap else None,
            "display_time": _to_local(str(event.get("timestamp", "")), self._tz_name),
        }

        resp = safe_http.send(
            self._method,
            self._url,
            allow_internal=self._allow_internal,
            json=payload,
            headers=self._headers,
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(
            "Webhook [%s] %s → %d",
            self._name, self._method, resp.status_code,
        )

    def _send_digest(self, report: dict) -> None:
        """Send digest report as structured JSON."""
        if not self._enabled:
            logger.warning(
                "Webhook [%s] digest suppressed - channel disabled",
                self._name,
            )
            return
        payload = {
            "type": "digest",
            "frequency": report.get("frequency"),
            "period": report.get("period_label"),
            "generated_at": report.get("generated_at"),
            "detections": report.get("detections"),
            "visits": report.get("visits"),
            "performance": report.get("performance"),
            "storage": report.get("storage"),
            "training": report.get("training"),
        }
        resp = safe_http.send(
            self._method, self._url, allow_internal=self._allow_internal,
            json=payload, headers=self._headers, timeout=15,
        )
        resp.raise_for_status()
        logger.info("Webhook [%s] digest sent → %d", self._name, resp.status_code)
