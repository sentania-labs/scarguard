"""Discord webhook dispatcher."""

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from snapshot_utils import annotate_snapshot
from url_safety import UnsafeURLError, validate_external_url

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


class DiscordNotifier:
    def __init__(self, cfg: dict, tz_name: str = "UTC") -> None:
        self._name: str = cfg.get("name", "discord")
        self._webhook_url: str = cfg["webhook_url"]
        self._mention_role: str = cfg.get("mention_role", "")
        self._include_snapshot: bool = cfg.get("include_snapshot", True)
        self._tz_name: str = tz_name
        # SSRF defence-in-depth — Discord's URL is a fixed pattern but we
        # validate anyway: an admin (or stolen session) could swap in a
        # private-IP URL and turn the channel into an SSRF probe.
        try:
            validate_external_url(self._webhook_url, allow_internal=False)
            self._enabled = True
        except UnsafeURLError as exc:
            logger.error(
                "Discord [%s] disabled — unsafe webhook URL: %s",
                self._name, exc,
            )
            self._enabled = False

    @property
    def name(self) -> str:
        return self._name

    def send(self, event: dict) -> None:
        if not self._enabled:
            logger.warning(
                "Discord [%s] suppressed — channel disabled at construction",
                self._name,
            )
            return
        if event.get("_digest"):
            self._send_digest(event)
            return

        class_name = event["class_name"]
        confidence = event["confidence"]
        camera_name = event["camera_name"]
        timestamp = _to_local(event["timestamp"], self._tz_name)
        snapshot_path: str | None = event.get("snapshot_path")

        mention = f"<@&{self._mention_role}> " if self._mention_role else ""
        content = (
            f"{mention}**{class_name.replace('_', ' ').title()} detected!**\n"
            f"Camera: `{camera_name}` | Confidence: `{confidence:.0%}` | `{timestamp}`"
        )

        base_url = event.get("_base_url", "")
        token = event.get("feedback_token", "")
        if base_url and token:
            fb = f"{base_url.rstrip('/')}/feedback/{token}"
            content += (
                f"\n[Correct]({fb}?v=correct) \u00b7 "
                f"[False Positive]({fb}?v=false_positive) \u00b7 "
                f"[Wrong Class]({fb}?v=wrong_class)"
            )

        payload: dict[str, object] = {"content": content}

        snapshot_bytes: bytes | None = None
        if self._include_snapshot and snapshot_path:
            data = annotate_snapshot(snapshot_path, event.get("bbox"), event.get("frame_size"))
            snapshot_bytes = data if data else None

        if snapshot_bytes and snapshot_path:
            # Embed the image in a Discord embed so it renders inline.
            filename = Path(snapshot_path).name
            payload["embeds"] = [{"image": {"url": f"attachment://{filename}"}}]
            resp = requests.post(
                self._webhook_url,
                data={"payload_json": json.dumps(payload)},
                files={"file": (filename, snapshot_bytes, "image/jpeg")},
                timeout=10,
            )
            resp.raise_for_status()
            logger.info("Discord notification sent with snapshot")
        else:
            resp = requests.post(
                self._webhook_url,
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
            logger.info("Discord notification sent (no snapshot)")

    def _send_digest(self, report: dict) -> None:
        """Send a digest report as a Discord embed."""
        if not self._enabled:
            logger.warning("Discord [%s] digest suppressed — disabled", self._name)
            return
        from digest import format_discord_embed

        payload = format_discord_embed(report)
        resp = requests.post(self._webhook_url, json=payload, timeout=15)
        resp.raise_for_status()
        logger.info("Discord digest sent")
