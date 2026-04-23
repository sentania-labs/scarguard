"""Webhook dispatcher — sends detection events as HTTP POST/PUT to a configured URL."""

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from url_safety import UnsafeURLError, validate_external_url

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
        # SSRF defence-in-depth — Pydantic validates at config save, but a
        # raw-YAML edit would bypass that. Validate again here so an
        # internal-pointing URL never reaches requests.request().
        allow_internal = bool(cfg.get("allow_internal", False))
        try:
            validate_external_url(self._url, allow_internal=allow_internal)
            self._enabled = True
        except UnsafeURLError as exc:
            logger.error(
                "Webhook [%s] disabled — unsafe URL %r: %s",
                self._name, self._url, exc,
            )
            self._enabled = False

    @property
    def name(self) -> str:
        return self._name

    def send(self, event: dict) -> None:
        if not self._enabled:
            logger.warning(
                "Webhook [%s] suppressed — channel disabled at construction",
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

    def _send_digest(self, report: dict) -> None:
        """Send digest report as structured JSON."""
        if not self._enabled:
            logger.warning(
                "Webhook [%s] digest suppressed — channel disabled",
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
        resp = requests.request(
            self._method, self._url, json=payload,
            headers=self._headers, timeout=15,
        )
        resp.raise_for_status()
        logger.info("Webhook [%s] digest sent → %d", self._name, resp.status_code)
