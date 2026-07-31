"""Security and validation helpers shared by the training UI and worker."""

from __future__ import annotations

import os
import stat
from pathlib import Path

ORIN_MIN_WORKERS = 0
ORIN_MAX_WORKERS = 4
ORIN_DEFAULT_WORKERS = 4


class ResumePathError(ValueError):
    """Raised when a requested checkpoint is not safe to resume."""


def validate_orin_workers(value: object) -> int:
    """Return a validated Jetson Orin data-loader worker count (0 through 4)."""
    try:
        workers = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("Training workers must be an integer from 0 through 4") from exc
    if not ORIN_MIN_WORKERS <= workers <= ORIN_MAX_WORKERS:
        raise ValueError("Training workers must be from 0 through 4 on the Jetson Orin profile")
    return workers


def validate_resume_checkpoint(raw_path: object, runs_root: Path) -> Path:
    """Validate a checkpoint as a real, non-symlink ``.pt`` file below runs_root.

    Both lexical and resolved containment are checked. Every existing component below
    ``runs_root`` is inspected with ``lstat`` so a symlinked run/weights directory is
    rejected even when it eventually resolves back beneath the workspace.
    """
    if not isinstance(raw_path, (str, os.PathLike)) or not str(raw_path).strip():
        raise ResumePathError("A checkpoint path is required")

    if runs_root.is_symlink():
        raise ResumePathError("Training runs directory must not be a symlink")
    try:
        root = runs_root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ResumePathError(f"Training runs directory does not exist: {runs_root}") from exc
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate

    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ResumePathError(f"Checkpoint must be beneath {root}") from exc

    current = root
    for component in relative.parts:
        current = current / component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError as exc:
            raise ResumePathError("Checkpoint does not exist") from exc
        if stat.S_ISLNK(mode):
            raise ResumePathError("Checkpoint path must not contain symlinks")

    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise ResumePathError(f"Checkpoint must resolve beneath {root}") from exc

    mode = resolved.stat().st_mode
    if not stat.S_ISREG(mode):
        raise ResumePathError("Checkpoint must be a regular file")
    if resolved.suffix.lower() != ".pt":
        raise ResumePathError("Checkpoint must be a .pt file")
    return resolved
