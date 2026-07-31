"""Narrow compatibility guard for Jetson's unsupported NVML process query.

PyTorch 2.4.0 in the pinned L4T base asks NVML for compute processes while it
is constructing a CUDA allocation error. Jetson reports that query as
unsupported and PyTorch raises an internal assertion instead of the useful
CUDA-OOM exception. Keep the original exception in the traceback, but make the
final exception accurately describe the allocation failure and include memory
figures that remain available.
"""

from __future__ import annotations

from typing import Any

_NVML_ASSERT = "NVML_SUCCESS == r INTERNAL ASSERT FAILED"
_ALLOCATOR_SOURCE = "CUDACachingAllocator.cpp"


def translate_masked_cuda_oom(exc: BaseException, torch_module: Any) -> BaseException | None:
    """Translate only the known PyTorch/Jetson diagnostic-masking assertion."""
    message = str(exc)
    if _NVML_ASSERT not in message or _ALLOCATOR_SOURCE not in message:
        return None

    details = "memory figures unavailable"
    try:
        free_bytes, total_bytes = torch_module.cuda.mem_get_info()
        allocated = torch_module.cuda.memory_allocated()
        reserved = torch_module.cuda.memory_reserved()
        mib = 1024 * 1024
        details = (
            f"{free_bytes / mib:.0f} MiB free / {total_bytes / mib:.0f} MiB total; "
            f"PyTorch allocated {allocated / mib:.0f} MiB and reserved {reserved / mib:.0f} MiB"
        )
    except Exception:
        pass

    oom_type = getattr(torch_module, "OutOfMemoryError", RuntimeError)
    return oom_type(
        "CUDA out of memory: a CUDA allocation failed; the pinned L4T/PyTorch 2.4.0 "
        "allocator's unsupported Jetson NVML process query masked its original diagnostic "
        f"({details}). The original NVML assertion is preserved as the chained cause."
    )
