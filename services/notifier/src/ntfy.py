"""Ntfy push notification dispatcher — sends detection alerts via ntfy.sh."""

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

logger = logging.getLogger(__name__)


def _to_local(iso_str: str, tz_name: str) -> str:
    """Convert a UTC ISO 8601 string to a formatted local-time string."""
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError, TypeError):
        tz = ZoneInfo("UTC")
    try:
        dt = datetime.fromisoformat(iso_str).astimezone(tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, TypeError):
        return str(iso_str)


class NtfyNotifier:
    """Sends detection alerts to an ntfy topic."""

    def __init__(self, cfg: dict, tz_name: str = "UTC") -> None:
        self._name: str = cfg.get("name", "ntfy")
        self._server: str = cfg.get("server", "https://ntfy.sh").rstrip("/")
        self._topic: str = cfg["topic"]
        self._token: str = cfg.get("token", "")
        self._username: str = cfg.get("username", "")
        self._password: str = cfg.get("password", "")
        self._priority: int = min(max(int(cfg.get("priority", 3)), 1), 5)
        self._include_snapshot: bool = cfg.get("include_snapshot", True)
        self._tz_name: str = tz_name

    @property
    def name(self) -> str:
        return self._name

    def send(self, event: dict) -> None:
        if event.get("_digest"):
            self._send_digest(event)
            return

        class_name = event["class_name"]
        confidence = event["confidence"]
        camera_name = event["camera_name"]
        timestamp = _to_local(event["timestamp"], self._tz_name)
        snapshot_path: str | None = event.get("snapshot_path")

        title = f"{class_name.replace('_', ' ').title()} detected!"
        message = f"Camera: {camera_name} | Confidence: {confidence:.0%} | {timestamp}"

        url = f"{self._server}/{self._topic}"
        headers: dict[str, str] = {
            "Title": title,
            "Priority": str(self._priority),
            "Tags": "warning" if self._priority >= 4 else "eyes",
        }

        base_url = event.get("_base_url", "")
        token = event.get("feedback_token", "")
        if base_url and token:
            fb = f"{base_url.rstrip('/')}/feedback/{token}"
            headers["Actions"] = (
                f"http, Correct, {fb}?v=correct, method=POST; "
                f"http, False Positive, {fb}?v=false_positive, method=POST; "
                f"http, Wrong Class, {fb}?v=wrong_class, method=POST"
            )

        self._add_auth_headers(headers)

        # Send with snapshot attachment if configured and file exists
        snap_file = Path(snapshot_path) if snapshot_path else None
        if self._include_snapshot and snap_file and snap_file.is_file():
            headers["Filename"] = snap_file.name
            headers["Message"] = message
            with open(snap_file, "rb") as f:
                resp = requests.put(url, data=f, headers=headers, timeout=10)
        else:
            resp = requests.post(url, data=message.encode(), headers=headers, timeout=10)

        resp.raise_for_status()
        logger.info("Ntfy [%s] → %s/%s (%d)", self._name, self._server, self._topic, resp.status_code)

    def _send_digest(self, report: dict) -> None:
        """Send condensed digest via ntfy."""
        from digest import format_plain_text

        text = format_plain_text(report)
        url = f"{self._server}/{self._topic}"
        headers: dict[str, str] = {
            "Title": f"ScarGuard Digest — {report.get('period_label', 'Summary')}",
            "Priority": "3",
            "Tags": "chart_with_upwards_trend",
        }
        self._add_auth_headers(headers)
        resp = requests.post(url, data=text.encode(), headers=headers, timeout=15)
        resp.raise_for_status()
        logger.info("Ntfy [%s] digest sent", self._name)

    def _add_auth_headers(self, headers: dict[str, str]) -> None:
        """Add authentication headers if credentials are configured."""
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        elif self._username and self._password:
            import base64
            credentials = base64.b64encode(f"{self._username}:{self._password}".encode()).decode()
            headers["Authorization"] = f"Basic {credentials}"
