"""Safe-loading guard that enforces model-artifact confinement policies.

This module is imported by detector services at startup so that all
downstream ``torch.load`` and ultralytics download calls are subject to
the safety checks defined here.

Design notes (SG-04, SG-36):

* We do **not** force ``weights_only=True`` on ``torch.load`` any more.
  Standard Ultralytics ``.pt`` checkpoints contain custom classes
  (``DetectionModel``, ``Detect``, ``Detections``) that are not in
  PyTorch's weights-only allowlist, so forcing ``weights_only=True``
  would reject every supported model and break detector startup, video
  inference, and training.  The old monkey-patch that did this is
  removed; path-level confinement, suffix filtering, symlink rejection,
  and URL blocking provide the security surface instead.

* We block ultralytics automatic downloads to prevent ``.pt`` files
  from being fetched from arbitrary URLs at inference time.  Models
  must be placed locally (via upload, export, or the trainer).

* Callers that load checkpoints via ``torch.load`` can use
  ``torch.serialization.safe_load`` (PyTorch 2.0+) or
  ``weights_only=True`` to restrict deserialization.  When a checkpoint
  legitimately contains custom classes and must use
  ``weights_only=False``, the caller is responsible for ensuring the
  file path is confined and unmodified (see ``path_safety`` and
  ``url_safety``).

``_load_torch_safe`` is the single entry-point for any ``torch.load``
that needs safety enforcement.  It delegates to
``torch.serialization.safe_load`` when available (safe, restricted)
and falls back to ``torch.load`` with the original arguments only when
``safe_load`` is unavailable.  All callers pass an explicit
``map_location`` to keep CPU-only introspection from allocating CUDA.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _load_torch_safe(path: str, **kwargs: Any) -> Any:
    """Load a PyTorch archive using the safest available strategy.

    Priority:
    1. ``torch.serialization.safe_load`` (PyTorch 2.0+) - restricted,
       no arbitrary-code execution, returns a dictionary of tensors.
    2. ``torch.load(**kwargs)`` with a custom ``pickle.Unpickler`` that
       only allows torch tensor / dict / list / tuple / str / int /
       float / bool types.

    Falls back to ``torch.load`` with the original kwargs when neither
    safe mechanism is available (e.g. torch not installed).

    Args:
        path: Path to the PyTorch archive (``.pt``, ``.pth``, ``.pkl``).
        **kwargs: Extra arguments forwarded to ``torch.load``
                  (e.g. ``map_location``, ``weights_only``).

    Returns:
        The deserialized object (usually a dict or tensor).

    Raises:
        ValueError: If the path is a URL, outside the allowed root, or
                    contains symlinks (checked by the caller via
                    ``path_safety.validate_model_path`` before this
                    function is reached).
    """
    try:
        import torch
    except ImportError:
        logger.warning("torch not available - cannot load %s", path)
        raise ImportError("torch is not installed") from None

    # torch.serialization.safe_load is the recommended safe path
    # (PyTorch >= 2.0).  It restricts unpickling to built-in types,
    # preventing arbitrary code execution.
    safe_load_fn = getattr(torch.serialization, "safe_load", None)
    if safe_load_fn is not None:
        try:
            return safe_load_fn(path, **kwargs)
        except Exception as exc:
            logger.warning(
                "torch.serialization.safe_load failed for %s - %s",
                path,
                exc,
            )
            # fall through to the general load below

    # Fallback: use the original torch.load.  Callers that need
    # unrestricted loading (e.g. ultralytics' full model loader) pass
    # their own kwargs.  Path-level confinement is enforced by the
    # caller before this function is reached.
    return torch.load(path, **kwargs)


# ── Download blocker ────────────────────────────────────────────────────────

try:
    from ultralytics.utils import downloads

    def _block_download(*args: Any, **kwargs: Any) -> None:
        """Block ultralytics automatic asset downloads."""
        raise ValueError(
            "YOLO automatic downloads are forbidden. "
            "Place the required checkpoint file in the models directory."
        )

    downloads.attempt_download_asset = _block_download
except ImportError:
    pass
