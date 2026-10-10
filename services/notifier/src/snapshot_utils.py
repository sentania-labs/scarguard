"""Snapshot annotation helpers shared by notifier channels."""

import io
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def annotate_snapshot(
    path: str,
    bbox: list[int] | None,
    frame_size: list[int] | None,
) -> bytes:
    """Return JPEG bytes with the detection bounding box drawn in red.

    Falls back to the raw (clean) file bytes if:
    - The file cannot be read.
    - bbox / frame_size data is absent or malformed.
    - Pillow is unavailable or raises an unexpected error.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        logger.warning("Snapshot not found or unreadable: %s", path)
        return b""

    if (
        not bbox
        or len(bbox) != 4
        or not frame_size
        or len(frame_size) != 2
    ):
        return raw

    try:
        from PIL import Image, ImageDraw

        img = Image.open(io.BytesIO(raw)).convert("RGB")
        # bbox is stored in the pixel space of the saved snapshot (frame_size).
        # The snapshot JPEG is always written at native frame resolution by the
        # detector, so img.size == frame_size and the coords are used directly.
        x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
        draw = ImageDraw.Draw(img)
        draw.rectangle([x1, y1, x2, y2], outline="#ff3333", width=3)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as exc:
        logger.warning("Failed to annotate snapshot, sending clean image: %s", exc)
        return raw
