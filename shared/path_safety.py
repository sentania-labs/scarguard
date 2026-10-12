import os
import stat
from pathlib import Path


def validate_model_path(raw_path: object, allowed_root: Path | str) -> Path:
    """Validate a path as a real, non-symlink file below allowed_root."""
    if not isinstance(raw_path, (str, os.PathLike)) or not str(raw_path).strip():
        raise ValueError("A model path is required")

    raw_str = str(raw_path)
    if "://" in raw_str or raw_str.startswith("http"):
        raise ValueError("URLs and automatic downloads are forbidden")

    root_path = Path(allowed_root)
    if root_path.is_symlink():
        raise ValueError("Root directory must not be a symlink")
    try:
        root = root_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"Root directory does not exist: {root_path}") from exc

    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate

    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path must be beneath {root}") from exc

    current = root
    for component in relative.parts:
        current = current / component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError as exc:
            raise ValueError("Path does not exist") from exc
        if stat.S_ISLNK(mode):
            raise ValueError("Path must not contain symlinks")

    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(f"Path must resolve beneath {root}") from exc

    mode = resolved.stat().st_mode
    if not stat.S_ISREG(mode):
        raise ValueError("Path must be a regular file")

    return resolved
