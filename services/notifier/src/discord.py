"""Discord webhook dispatcher."""

import json
import logging
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


class DiscordNotifier:
    def __init__(self, cfg: dict) -> None:
        self._webhook_url: str = cfg["webhook_url"]
        self._mention_role: str = cfg.get("mention_role", "")
        self._include_snapshot: bool = cfg.get("include_snapshot", True)

    def send(self, event: dict) -> None:
        class_name = event["class_name"]
        confidence = event["confidence"]
        camera_name = event["camera_name"]
        timestamp = event["timestamp"]
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
