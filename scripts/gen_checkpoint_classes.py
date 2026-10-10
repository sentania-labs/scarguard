#!/usr/bin/env python3
"""Regenerate shared/checkpoint_classes.py: layer classes a model checkpoint may name.

The candidate validator (shared/model_store.py) only lets a checkpoint pickle
instantiate classes listed there.  Run once per pinned torch/ultralytics pair
and merge (union) the results, e.g.::

    YOLO_CONFIG_DIR=/tmp/yolo uv run --no-project --with torch==2.4.0 --with ultralytics==8.4.56 \\
        python scripts/gen_checkpoint_classes.py > /tmp/a.txt
    YOLO_CONFIG_DIR=/tmp/yolo uv run --no-project --with torch==2.4.0 --with "ultralytics~=8.3.0" \\
        python scripts/gen_checkpoint_classes.py > /tmp/b.txt

A class qualifies when it is an ``nn.Module`` subclass defined (not merely
re-exported) in ``torch.nn.modules`` or ``ultralytics.nn``, and unpickling it
cannot run class-specific code: ``__new__``, ``__reduce__``,
``__reduce_ex__``, ``__getattr__`` and ``__setattr__`` resolve to
``nn.Module``/``object``, and ``__setstate__`` resolves to ``nn.Module`` or to
one of the reviewed overrides in BENIGN_SETSTATE (each only fills in a
missing attribute default).  Modules that load, download or install weights
or packages by design are skipped (importing text_model can trigger an
Ultralytics auto-install).
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import torch
import ultralytics
import ultralytics.nn
from torch import nn

SKIP_MODULES = ("ultralytics.nn.autobackend", "ultralytics.nn.backends", "ultralytics.nn.text_model")
HOOKS = ("__new__", "__reduce__", "__reduce_ex__", "__getattr__", "__setattr__", "__setstate__")
# Reviewed __setstate__ overrides: each calls Module.__setstate__ and sets a default.
BENIGN_SETSTATE = {
    "torch.nn.modules.conv._ConvNd",
    "torch.nn.modules.upsampling.Upsample",
    "torch.nn.modules.activation.MultiheadAttention",
    "torch.nn.modules.activation.Softmax",
    "torch.nn.modules.activation.Softmin",
    "torch.nn.modules.activation.LogSoftmax",
    "torch.nn.modules.pooling.AvgPool3d",
    "torch.nn.modules.transformer.TransformerEncoderLayer",
    "torch.nn.modules.transformer.TransformerDecoderLayer",
    "ultralytics.nn.modules.block.AAttn",
}


def _definer(cls: type, hook: str) -> str:
    for base in cls.__mro__:
        if hook in vars(base):
            return f"{base.__module__}.{base.__qualname__}"
    return "builtins.object"


def _safe(cls: type) -> bool:
    for hook in HOOKS:
        definer = _definer(cls, hook)
        if definer in ("torch.nn.modules.module.Module", "builtins.object"):
            continue
        if hook == "__setstate__" and definer in BENIGN_SETSTATE:
            continue
        return False
    return True


def _modules() -> list[str]:
    names = ["torch.nn.modules"] + [
        m.name for m in pkgutil.walk_packages(torch.nn.modules.__path__, "torch.nn.modules.")
    ]
    names += ["ultralytics.nn"] + [
        m.name for m in pkgutil.walk_packages(ultralytics.nn.__path__, "ultralytics.nn.")
    ]
    return [n for n in names if not n.startswith(SKIP_MODULES)]


def main() -> None:
    found: set[tuple[str, str]] = set()
    for name in _modules():
        try:
            module = importlib.import_module(name)
        except Exception:  # optional dependency missing - nothing to allow
            continue
        for attr, value in vars(module).items():
            if not inspect.isclass(value) or value.__module__ != name:
                continue
            if not issubclass(value, nn.Module) or value is nn.Module:
                continue
            if not _safe(value):
                continue
            found.add((name, attr))
    print(f"# torch {torch.__version__}, ultralytics {ultralytics.__version__}")
    for module, attr in sorted(found):
        print(f"{module} {attr}")


if __name__ == "__main__":
    main()
