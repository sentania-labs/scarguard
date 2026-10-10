import logging

logger = logging.getLogger(__name__)

try:
    import torch
    _orig_load = torch.load

    def _safe_torch_load(*args, **kwargs):
        kwargs["weights_only"] = True
        try:
            return _orig_load(*args, **kwargs)
        except Exception as exc:
            if "weights_only" in str(exc) or "Unsupported" in str(exc) or "pickle" in str(exc).lower():
                raise ValueError(
                    "Unrestricted pickle loading is forbidden. "
                    "This checkpoint requires arbitrary code execution to load. "
                    "Please convert it to a safe weights-only format or replace it."
                ) from exc
            raise

    torch.load = _safe_torch_load
except ImportError:
    pass

try:
    from ultralytics.utils import downloads
    def _block_download(*args, **kwargs):
        raise ValueError("YOLO automatic downloads are forbidden.")
    downloads.attempt_download_asset = _block_download
except ImportError:
    pass
