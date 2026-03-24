"""Discord webhook dispatcher."""

import json
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


class DiscordNotifier:
    def __init__(self, cfg: dict, tz_name: str = "UTC") -> None:
        self._webhook_url: str = cfg["webhook_url"]
        self._mention_role: str = cfg.get("mention_role", "")
        self._include_snapshot: bool = cfg.get("include_snapshot", True)
        self._tz_name: str = tz_name

    def send(self, event: dict) -> None:
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

        payload: dict[str, object] = {"content": content}

        snapshot_bytes: bytes | None = None
        if self._include_snapshot and snapshot_path:
            snapshot_bytes = _read_snapshot(snapshot_path)

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


def _read_snapshot(path: str) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        logger.warning("Snapshot not found or unreadable: %s", path)
        return None
