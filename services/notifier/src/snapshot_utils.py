"""Snapshot loading and annotation helpers shared by notifier channels.

Detection events name a ``snapshot_path``. Before any channel attaches it,
:func:`load_snapshot` checks that the file:

* resolves (symlinks included) to a regular file inside the snapshot root
  (``SNAPSHOT_DIR``, default ``/data/snapshots``);
* has an image suffix (``.jpg``, ``.jpeg``, ``.png``) and its bytes really
  are an image of that format (Pillow parses and verifies it);
* is no larger than ``MAX_SNAPSHOT_BYTES``.

Anything else is refused and the notification goes out without an image, so
an event (or a symlink planted in the snapshot directory) can never make a
channel upload an arbitrary file such as ``/config/scarguard.yml``.
"""

import io
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_SNAPSHOT_DIR = "/data/snapshots"
MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024
# Annotation decodes the full frame; refuse decompression bombs up front.
MAX_SNAPSHOT_PIXELS = 40_000_000

# Suffix → Pillow format name the content must decode as.
_IMAGE_FORMATS: dict[str, str] = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
}


@dataclass(frozen=True)
class Snapshot:
    """A validated snapshot image: its bytes, file name and Pillow format."""

    data: bytes
    filename: str
    format: str


def snapshot_root() -> Path:
    """Resolved snapshot root; read per call so a config/env change applies."""
    return Path(os.environ.get("SNAPSHOT_DIR", DEFAULT_SNAPSHOT_DIR)).resolve()


def load_snapshot(path: str) -> Snapshot | None:
    """Return the validated snapshot at *path*, or ``None`` (logged) if unsafe."""
    try:
        root = snapshot_root()
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        logger.warning("Snapshot not found or unreadable: %s", path)
        return None
    if not resolved.is_relative_to(root) or resolved == root:
        logger.warning("Snapshot %s is outside the snapshot directory %s - not attaching", path, root)
        return None
    expected = _IMAGE_FORMATS.get(resolved.suffix.lower())
    if expected is None:
        logger.warning("Snapshot %s is not a .jpg/.jpeg/.png file - not attaching", path)
        return None

    # O_NOFOLLOW: the final component was resolved above and must not have
    # become a symlink since. O_NONBLOCK: never hang on a FIFO.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(resolved, flags)
    except OSError:
        logger.warning("Snapshot not found or unreadable: %s", path)
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            logger.warning("Snapshot %s is not a regular file - not attaching", path)
            return None
        if st.st_size > MAX_SNAPSHOT_BYTES:
            logger.warning("Snapshot %s exceeds %d bytes - not attaching", path, MAX_SNAPSHOT_BYTES)
            return None
        with os.fdopen(os.dup(fd), "rb") as fh:
            data = fh.read(MAX_SNAPSHOT_BYTES + 1)
    except OSError:
        logger.warning("Snapshot not found or unreadable: %s", path)
        return None
    finally:
        os.close(fd)
    if len(data) > MAX_SNAPSHOT_BYTES:
        logger.warning("Snapshot %s exceeds %d bytes - not attaching", path, MAX_SNAPSHOT_BYTES)
        return None

    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            actual = img.format
            width, height = img.size
            img.verify()
    except Exception as exc:
        logger.warning("Snapshot %s is not a valid image (%s) - not attaching", path, exc)
        return None
    if width * height > MAX_SNAPSHOT_PIXELS:
        logger.warning("Snapshot %s is %dx%d pixels - not attaching", path, width, height)
        return None
    if actual != expected:
        logger.warning(
            "Snapshot %s content is %s but its suffix says %s - not attaching",
            path, actual, expected,
        )
        return None
    return Snapshot(data=data, filename=resolved.name, format=expected)


def annotate_snapshot(
    path: str,
    bbox: list[int] | None,
    frame_size: list[int] | None,
) -> bytes:
    """Return the validated snapshot with the detection bounding box drawn in red.

    Returns ``b""`` if the snapshot fails :func:`load_snapshot`. Falls back
    to the raw (clean) validated bytes if bbox / frame_size data is absent or
    malformed, or Pillow raises while drawing.
    """
    snap = load_snapshot(path)
    if snap is None:
        return b""
    return annotate_bytes(snap.data, bbox, frame_size)


def annotate_bytes(
    raw: bytes,
    bbox: list[int] | None,
    frame_size: list[int] | None,
) -> bytes:
    """Draw *bbox* on already-validated image bytes; JPEG output when drawn."""
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
