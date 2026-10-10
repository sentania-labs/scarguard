"""Model candidate store: training and uploads never write the live model (SG-07).

Training jobs and web uploads publish *candidates* into a store that is
separate from ``MODELS_DIR`` (the directory the detector and the Config
model picker read).  An administrator then explicitly promotes a candidate
onto a file name in ``MODELS_DIR``; the previous file of that name is first
copied into a rollback slot, and every promotion, rollback and discard is
appended to a durable history ledger.

Layout below ``MODEL_STORE_DIR`` (default ``/data/model_store``)::

    .staging/<id>/          in-progress publication; never listed
    candidates/<id>/        model<suffix> + manifest.json (immutable)
    rollback/<id>/          model<suffix> + manifest.json (pre-promotion copy)
    history.jsonl           append-only promotion/rollback/discard ledger
    .lock                   flock serialising promotions and rollbacks

Every publication writes the bytes, fsyncs them, validates them, writes an
fsynced manifest and only then renames the staging directory into place and
fsyncs the parent, so an interrupted job or crashed web request leaves at
most a ``.staging`` directory and never a half-written candidate or live
model.  Promotion copies the verified candidate to a temporary file inside
``MODELS_DIR`` and ``os.replace``s it onto the target name.

Validation never unpickles anything.  ``.pt`` files must be PyTorch zip
archives whose pickle only references an allowlist of globals used by
Ultralytics/PyTorch checkpoints; ``.onnx`` and ``.engine`` files receive
structural checks only (see ``validate_model_file``).  This is
defence-in-depth for files an administrator chose to upload, not a sandbox.

Live files whose bytes do not match a recorded promotion keep the
provenance status ``unresolved``; nothing here infers where they came from.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import pickletools
import re
import shutil
import stat
import struct
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator, NamedTuple

from checkpoint_classes import LAYER_CLASSES

logger = logging.getLogger(__name__)

ALLOWED_SUFFIXES = (".pt", ".engine", ".onnx")
MANIFEST_NAME = "manifest.json"
HISTORY_NAME = "history.jsonl"
UNRESOLVED = "unresolved"
_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COPY_CHUNK = 1024 * 1024
_MAX_PICKLE_BYTES = 64 * 1024 * 1024
# Archive bounds, all read from the central directory before any member is
# inflated.  torch.save stores members uncompressed, so a real checkpoint
# expands to about its own size; a deflate bomb declares far more.
_MAX_ZIP_MEMBERS = 16384
_MAX_ZIP_CENTRAL_DIR_BYTES = _MAX_ZIP_MEMBERS * 512  # zipfile reads it whole
_MAX_ZIP_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
_MAX_ZIP_EXPANSION_RATIO = 8
_ZIP_EXPANSION_FLOOR_BYTES = 4 * 1024 * 1024  # tiny archives: ratio does not apply
_SECRET_KEY_TOKENS = ("password", "secret", "token", "api_key")

# Checkpoint pickle policy (see audit_pickle).  Kinds of global:
#
# * Layer classes - the generated LAYER_CLASSES list (real nn.Module classes
#   whose unpickling runs no class-specific code).  Only ever instantiated
#   with NEWOBJ (``cls.__new__``, never ``__init__``) and never called.
# * Call targets - the few callables torch.save output actually invokes,
#   each with an argument check in audit_pickle.
# * References - names that may appear as values (storage types, dtypes,
#   ``numpy.ndarray`` for _reconstruct) but are never called:
#   ndarray(shape, dtype("O"), buffer) would forge object pointers.
# Anything else (os.system, builtins.eval, module re-exports such as
# ultralytics.nn.tasks.pickle, singletons such as DEFAULT_CFG_DICT, ...)
# rejects the file.
_NUMPY_MULTIARRAY = ("numpy.core.multiarray", "numpy._core.multiarray")
_CALL_TARGETS = {
    ("collections", "OrderedDict"),
    ("builtins", "set"), ("__builtin__", "set"),
    ("builtins", "frozenset"), ("__builtin__", "frozenset"),
    ("builtins", "bytes"), ("__builtin__", "bytes"),
    ("_codecs", "encode"), ("codecs", "encode"),
    ("torch", "Size"),
    ("torch", "device"),
    ("torch._utils", "_rebuild_tensor_v2"),
    ("torch._utils", "_rebuild_parameter"),
    ("numpy", "dtype"),
    *((module, "_reconstruct") for module in _NUMPY_MULTIARRAY),
    *((module, "scalar") for module in _NUMPY_MULTIARRAY),
    ("ultralytics.utils", "IterableSimpleNamespace"),
    ("types", "SimpleNamespace"),
    ("argparse", "Namespace"),
    ("pathlib", "PosixPath"),
    ("pathlib", "WindowsPath"),
    ("builtins", "getattr"), ("__builtin__", "getattr"),
}
_REFERENCES = {("numpy", "ndarray")}
# The only getattr torch.save emits for these models: Segment/Pose/OBB keep
# ``self.detect = Detect.forward``.  (owner module, owner class, attr, dict key)
_LAYER_ATTRS = {("ultralytics.nn.modules.head", "Detect", "forward", "detect")}
# torch.HalfStorage ... and their element sizes, for persistent-id checks.
_STORAGE_ITEMSIZE = {
    "DoubleStorage": 8, "FloatStorage": 4, "HalfStorage": 2, "BFloat16Storage": 2,
    "LongStorage": 8, "IntStorage": 4, "ShortStorage": 2, "CharStorage": 1,
    "ByteStorage": 1, "BoolStorage": 1,
}
# torch.float16 ... as values only.
_TORCH_DTYPE_RE = re.compile(r"^(?:u?int(?:8|16|32|64)|float(?:16|32|64)|bfloat16|bool|half)$")
_NUMPY_CODE_RE = re.compile(r"^[<>|=]?(?:[fiu][1248]|b1|\?)$")
_LOCATION_RE = re.compile(r"^(?:cpu|cuda(?::\d{1,2})?)$")
# A model's ``yaml`` is re-parsed by Ultralytics when it is trained from (the
# trainer's base model): ``eval(yaml["activation"])`` and ``globals()[name]``
# for each layer.  Only these activations and known layer names are accepted.
# Layers whose __init__ downloads weights when a yaml names them (torchvision registry).
_YAML_DENIED_NAMES = {"TorchVision"}
# Ultralytics keeps train_args / model.args values such as ``data`` and passes
# them to check_file(), which downloads URLs.
_ARGS_KEYS = {"train_args", "args"}
_URL_RE = re.compile(r":/|^(?:ul|gs|s3)//", re.IGNORECASE)
_SAFE_ACTIVATIONS = {
    "nn.SiLU()", "nn.ReLU()", "nn.LeakyReLU(0.1)", "nn.Hardswish()",
    "torch.nn.SiLU()", "torch.nn.ReLU()",
}
_STRING_OPS = {
    "SHORT_BINUNICODE", "BINUNICODE", "BINUNICODE8", "UNICODE",
    "SHORT_BINSTRING", "BINSTRING", "STRING",
}
_MEMO_PUT_OPS = {"PUT", "BINPUT", "LONG_BINPUT"}
_PROMOTE_TMP_SUFFIX = ".promote.tmp"
_STAGING_MAX_AGE_SECONDS = 24 * 3600
# Ledger actions that put bytes at a live name, and the label shown for them.
_INSTALL_ACTIONS = {
    "promote": "promote",
    "promote_started": "promote",
    "rollback": "rollback",
    "rollback_started": "rollback",
}
_MEMO_GET_OPS = {"GET", "BINGET", "LONG_BINGET"}


class ModelStoreError(Exception):
    """A store operation failed; the live model was left unchanged."""


class CandidateValidationError(ModelStoreError):
    """A model file failed validation and was not published."""


# ── helpers ─────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json_durable(path: Path, payload: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o644)


def _copy_hashing(src: Path, dest: Path) -> tuple[str, int]:
    """Copy *src* to a new *dest*, fsync it and return (sha256, size)."""
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(src, flags)
    with os.fdopen(source_fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ModelStoreError("Model source must be a regular file")
        with open(dest, "xb") as target:
            while chunk := source.read(_COPY_CHUNK):
                digest.update(chunk)
                size += len(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    os.chmod(dest, 0o644)
    return digest.hexdigest(), size


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_COPY_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def redact_config(value: Any, key: str = "") -> Any:
    """Return *value* with credential-like keys replaced by a fixed marker."""
    if isinstance(value, dict):
        return {str(k): redact_config(v, str(k).lower()) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_config(v, key) for v in value]
    if any(token in key for token in _SECRET_KEY_TOKENS) and value not in (None, ""):
        return "***"
    return value


def config_digest(value: Any) -> str:
    """Stable SHA256 of a redacted config fragment, for provenance."""
    rendered = json.dumps(redact_config(value), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def safe_model_name(name: object, suffix: str | None = None) -> str:
    """Validate a bare model file name for MODELS_DIR; raise ValueError if unsafe."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError(
            "Model name must be 1-128 characters of letters, digits, '.', '_' or '-' "
            "and must not start with '.'"
        )
    actual = Path(name).suffix.lower()
    if actual not in ALLOWED_SUFFIXES:
        raise ValueError(f"Model name must end with one of {', '.join(ALLOWED_SUFFIXES)}")
    if suffix is not None and actual != suffix:
        raise ValueError(f"Model name must keep the candidate's {suffix} suffix")
    return name


# ── validation ──────────────────────────────────────────────────────────────


class _Global(NamedTuple):
    module: str
    name: str


class _Tuple(NamedTuple):
    items: tuple[object, ...]


class _LayerAttr(NamedTuple):
    """``getattr(Detect, "forward")`` - see _LAYER_ATTRS."""

    owner: _Global
    name: str


class _Box:
    """A container or object the stream built (identity matters, so not a tuple).

    ``kind`` is dict, list, set, frozenset, storage, bytes, tensor, or an
    object kind: layer, namespace, odict, dtype, ndarray, value.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.size = 0
        self.children: list[_Box] = []
        self.sealed = False  # contents fixed: already handed to a call or BUILD
        self.layer_attr_state = False  # holds a _LayerAttr; may only be BUILD state
        self.used_as_state = False
        self.numel = 0  # storage boxes: element count
        self.entries: list[tuple[object, object]] = []  # dict/odict contents
        self.elements: list[object] = []  # list contents


_GETATTR = {_Global("builtins", "getattr"), _Global("__builtin__", "getattr")}
_STATE_DICT_KINDS = {"layer", "namespace", "odict"}
_CONTAINER_KINDS = {"dict", "odict", "list", "set"}
_PRIM_OPS = {
    "NONE", "NEWTRUE", "NEWFALSE", "INT", "BININT", "BININT1", "BININT2", "LONG", "LONG1",
    "LONG4", "FLOAT", "BINFLOAT",
}
_BYTES_OPS = {"BINBYTES", "SHORT_BINBYTES", "BINBYTES8"}
_LITERALS: dict[str, object] = {"NONE": None, "NEWTRUE": True, "NEWFALSE": False}


def _is_layer_class(value: object) -> bool:
    return isinstance(value, _Global) and (value.module, value.name) in LAYER_CLASSES


def _is_reference(value: object) -> bool:
    """A global that may be stored as data (never called)."""
    return isinstance(value, _Global) and (
        (value.module, value.name) in _REFERENCES
        or (value.module == "torch" and bool(_TORCH_DTYPE_RE.fullmatch(value.name)))
    )


_TORCH_LAYER_NAMES = {n for m, n in LAYER_CLASSES if m.startswith("torch.nn.modules.")}
_ULTRALYTICS_LAYER_NAMES = {n for m, n in LAYER_CLASSES if m.startswith("ultralytics.nn.")}


def _known_layer_name(name: object) -> bool:
    """A yaml module name Ultralytics resolves to an allowlisted layer class."""
    if not isinstance(name, str):
        return False
    if name.startswith("nn."):
        return name[3:] in _TORCH_LAYER_NAMES
    return name in _ULTRALYTICS_LAYER_NAMES and name not in _YAML_DENIED_NAMES


def _global_allowed(module: str, name: str) -> bool:
    if not name or "." in name:
        return False  # protocol-4 dotted names walk attributes to arbitrary objects
    key = (module, name)
    if key in _CALL_TARGETS or key in _REFERENCES or key in LAYER_CLASSES:
        return True
    return module == "torch" and (
        name in _STORAGE_ITEMSIZE or bool(_TORCH_DTYPE_RE.fullmatch(name))
    )


def _plain(value: object) -> bool:
    """A literal: str, number, bool, None, bytes, or a tuple of those."""
    if isinstance(value, _Tuple):
        return all(_plain(item) for item in value.items)
    if isinstance(value, _Box):
        return value.kind == "bytes"
    return value is None or isinstance(value, (str, int, float))


def _ints(value: object) -> list[int] | None:
    if isinstance(value, _Tuple) and all(
        isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in value.items
    ):
        return [int(v) for v in value.items]  # type: ignore[call-overload]
    return None


class _PickleAudit:
    """One pass over a pickle stream; see ``audit_pickle``."""

    def __init__(self) -> None:
        self.stack: list[object] = []
        self.marks: list[int] = []
        self.memo: dict[int, object] = {}
        self.found: list[tuple[str, str]] = []
        self.problems: list[str] = []
        self.storages: dict[str, tuple[str, int]] = {}  # key -> (storage type, numel)
        self.state_boxes: list[_Box] = []
        self.layer_states: list[_Box] = []
        self.args_values: list[object] = []  # train_args / model.args / namespace state
        self.proto = 0

    # ── stack ──

    def pop(self) -> object:
        if not self.stack or (self.marks and len(self.stack) <= self.marks[-1]):
            raise CandidateValidationError("Checkpoint pickle stack underflow")
        return self.stack.pop()

    def pop_mark(self) -> list[object]:
        if not self.marks:
            raise CandidateValidationError("Checkpoint pickle has no MARK to pop")
        mark = self.marks.pop()
        items = self.stack[mark:]
        del self.stack[mark:]
        return items

    def top(self) -> object:
        return self.stack[-1] if self.stack else None

    # ── value rules ──

    def seal(self, value: object) -> None:
        if isinstance(value, _Tuple):
            for item in value.items:
                self.seal(item)
        elif isinstance(value, _Box) and not value.sealed:
            value.sealed = True
            for child in value.children:
                self.seal(child)

    def data(self, values: list[object], where: str) -> None:
        """Values passed as plain data (call args, container items): no code."""
        for value in values:
            if isinstance(value, _Tuple):
                self.data(list(value.items), where)
            elif isinstance(value, _Global) and not _is_reference(value):
                self.problems.append(f"{value.module}.{value.name} used as {where}")
            elif isinstance(value, _LayerAttr):
                self.problems.append(f"layer method used as {where}")
            elif isinstance(value, _Box) and value.layer_attr_state:
                self.problems.append(f"dict holding a layer method used as {where}")
        self.seal(_Tuple(tuple(values)))

    def adopt(self, parent: _Box, values: list[object]) -> None:
        for value in values:
            if isinstance(value, _Box):
                parent.children.append(value)
            elif isinstance(value, _Tuple):
                self.adopt(parent, list(value.items))

    def mutate(self, target: object, kind: tuple[str, ...], opname: str) -> _Box | None:
        if not isinstance(target, _Box) or target.kind not in kind:
            self.problems.append(f"{opname} on something other than a {'/'.join(kind)}")
            return None
        if target.sealed:
            self.problems.append(f"{opname} on a container after it was used")
        return target

    # ── opcodes ──

    def call(self, target: object, args: object, how: str) -> object:
        """Check one call; return the symbolic result."""
        if not isinstance(target, _Global) or (target.module, target.name) not in _CALL_TARGETS:
            label = f"{target.module}.{target.name}" if isinstance(target, _Global) else "a value"
            self.problems.append(f"{how} calls {label}, which is not an allowed call target")
            return _Box("value")
        if not isinstance(args, _Tuple):
            self.problems.append(f"{how} arguments are not a tuple")
            return _Box("value")
        items = args.items
        key = (target.module, target.name)
        if target in _GETATTR:
            owner, attr = (items + (None, None))[:2]
            if len(items) == 2 and isinstance(owner, _Global) and isinstance(attr, str):
                if any((owner.module, owner.name, attr) == entry[:3] for entry in _LAYER_ATTRS):
                    return _LayerAttr(owner, attr)
            self.problems.append("getattr other than the Detect.forward pattern")
            return _Box("value")
        if key[1] == "encode":
            if len(items) == 2 and isinstance(items[0], str) and items[1] in ("latin1", "latin-1"):
                return _Box("bytes")
            self.problems.append("codecs.encode outside latin1 bytes")
            return _Box("value")
        if key[1] == "bytes":
            if not items:
                return _Box("bytes")
            self.problems.append("bytes() with arguments")
            return _Box("value")
        if key in (
            ("ultralytics.utils", "IterableSimpleNamespace"),
            ("types", "SimpleNamespace"),
            ("argparse", "Namespace"),
            ("collections", "OrderedDict"),
        ):
            if items:
                self.problems.append(f"{key[1]} constructed with arguments")
            return _Box("odict" if key[1] == "OrderedDict" else "namespace")
        if key == ("numpy", "dtype"):
            if (
                len(items) == 3
                and isinstance(items[0], str)
                and _NUMPY_CODE_RE.fullmatch(items[0])
                and all(isinstance(v, (bool, int)) for v in items[1:])
            ):
                return _Box("dtype")
            self.problems.append("numpy.dtype other than a plain numeric type")
            return _Box("value")
        if key[1] == "_reconstruct":
            if (
                len(items) == 3
                and items[0] == _Global("numpy", "ndarray")
                and _plain(items[1])
                and _plain(items[2])
            ):
                return _Box("ndarray")
            self.problems.append("numpy _reconstruct outside the plain ndarray pattern")
            return _Box("value")
        if key[1] == "scalar":
            dtype, raw = (items + (None, None))[:2]
            if len(items) == 2 and isinstance(dtype, _Box) and dtype.kind == "dtype" and (
                isinstance(raw, _Box) and raw.kind == "bytes"
            ):
                self.seal(dtype)
                return _Box("value")
            self.problems.append("numpy scalar outside the numeric dtype pattern")
            return _Box("value")
        if key[1] == "_rebuild_tensor_v2":
            return self.rebuild_tensor(items)
        if key[1] == "_rebuild_parameter":
            data, grad, hooks = (items + (None, None, None))[:3]
            if (
                len(items) == 3
                and isinstance(data, _Box)
                and data.kind == "tensor"
                and isinstance(grad, bool)
                and self.empty_hooks(hooks)
            ):
                self.seal(data)
                return _Box("tensor")
            self.problems.append("_rebuild_parameter outside the plain pattern")
            return _Box("value")
        # set/frozenset/torch.Size/torch.device/PosixPath: plain data only.
        for item in items:
            if not (_plain(item) or (isinstance(item, _Box) and item.kind == "list")):
                self.problems.append(f"{key[0]}.{key[1]} called with a non-literal argument")
        self.data(list(items), f"{key[1]} argument")
        return _Box("value")

    def empty_hooks(self, hooks: object) -> bool:
        ok = isinstance(hooks, _Box) and hooks.kind == "odict" and hooks.size == 0
        self.seal(hooks)
        return ok

    def rebuild_tensor(self, items: tuple[object, ...]) -> object:
        storage, offset, size, stride, grad, hooks = (items + (None,) * 6)[:6]
        dims, steps = _ints(size), _ints(stride)
        if (
            len(items) == 6
            and isinstance(storage, _Box)
            and storage.kind == "storage"
            and isinstance(offset, int)
            and not isinstance(offset, bool)
            and offset >= 0
            and dims is not None
            and steps is not None
            and len(dims) == len(steps)
            and isinstance(grad, bool)
            and self.empty_hooks(hooks)
        ):
            if 0 in dims:
                in_bounds = offset <= storage.numel
            else:
                in_bounds = offset + sum((d - 1) * st for d, st in zip(dims, steps)) < storage.numel
            if in_bounds:
                return _Box("tensor")
            self.problems.append("tensor view exceeds its storage")
            return _Box("value")
        self.problems.append("_rebuild_tensor_v2 outside the plain pattern")
        return _Box("value")

    def persistent(self, pid: object) -> object:
        items = pid.items if isinstance(pid, _Tuple) else ()
        kind, typ, key, location, numel = (items + (None,) * 5)[:5]
        if (
            len(items) == 5
            and kind == "storage"
            and isinstance(typ, _Global)
            and typ.module == "torch"
            and typ.name in _STORAGE_ITEMSIZE
            and isinstance(key, str)
            and key.isdigit()
            and isinstance(location, str)
            and _LOCATION_RE.fullmatch(location)
            and isinstance(numel, int)
            and not isinstance(numel, bool)
            and numel >= 0
        ):
            known = self.storages.setdefault(key, (typ.name, numel))
            if known != (typ.name, numel):
                self.problems.append(f"storage {key} is described two different ways")
            box = _Box("storage")
            box.numel = numel
            return box
        self.problems.append("persistent id other than a plain torch storage")
        return _Box("value")

    def build(self, target: object, state: object) -> None:
        kind = target.kind if isinstance(target, _Box) else None
        ok = False
        if kind in _STATE_DICT_KINDS:
            parts = state.items if isinstance(state, _Tuple) and len(state.items) == 2 else (state,)
            ok = all(
                part is None or (isinstance(part, _Box) and part.kind == "dict") for part in parts
            )
            for part in parts:
                if isinstance(part, _Box):
                    part.used_as_state = True
                    self.seal(part)
                    if kind == "layer":
                        self.layer_states.append(part)
                    elif kind == "namespace":
                        self.args_values.append(part)
        elif kind == "dtype":
            ok = (
                isinstance(state, _Tuple)
                and len(state.items) == 8
                and state.items[0] == 3
                and state.items[1] in ("<", ">", "|", "=")
                and state.items[2:5] == (None, None, None)
                and state.items[5:] == (-1, -1, 0)
            )
        elif kind == "ndarray":
            items = state.items if isinstance(state, _Tuple) else ()
            ok = (
                len(items) == 5
                and items[0] == 1
                and _plain(items[1])
                and isinstance(items[2], _Box)
                and items[2].kind == "dtype"
                and isinstance(items[3], bool)
                and _plain(items[4])
            )
            if ok:
                self.seal(items[2])
        if not ok:
            self.problems.append(f"BUILD on {kind or 'a non-object'} with an unexpected state")

    def set_items(self, target: object, pairs: list[object]) -> None:
        if len(pairs) % 2:
            raise CandidateValidationError("Checkpoint pickle has an odd SETITEMS slice")
        box = self.mutate(target, ("dict", "odict"), "SETITEM")
        if box is None:
            return
        for key, value in zip(pairs[0::2], pairs[1::2]):
            box.size += 1
            if not isinstance(key, (str, int)) or isinstance(key, bool):
                self.problems.append("dict key that is not a str or int")
            elif isinstance(key, str) and key.startswith("__"):
                self.problems.append(f"dict key {key[:40]!r} is not allowed")
            if isinstance(value, _LayerAttr):
                entry = (value.owner.module, value.owner.name, value.name, key)
                if box.kind != "dict" or entry not in _LAYER_ATTRS:
                    self.problems.append("layer method stored outside its known attribute")
                box.layer_attr_state = True
                self.state_boxes.append(box)
            else:
                self.data([value], "dict value")
            self.adopt(box, [value])
            box.entries.append((key, value))
            if key in _ARGS_KEYS:
                self.args_values.append(value)

    # ── driver ──

    def run(self, data: bytes) -> None:
        for opcode, arg, _pos in pickletools.genops(data):
            name = opcode.name
            if name == "STOP":
                break
            handler = getattr(self, f"op_{name}", None)
            if name in _STRING_OPS:
                self.stack.append(str(arg))
            elif name in _PRIM_OPS:
                self.stack.append(_LITERALS[name] if name in _LITERALS else arg)
            elif name in _BYTES_OPS:
                self.stack.append(_Box("bytes"))
            elif handler is not None:
                handler(arg)
            else:
                self.problems.append(f"pickle opcode {name} is not allowed")
                break
        for box in self.state_boxes:
            if not box.used_as_state:
                self.problems.append("dict holding a layer method was not used as BUILD state")
        for state in self.layer_states:
            for key, value in state.entries:
                if key == "yaml":
                    self.check_model_yaml(value)
        if any(self.has_url(value, set()) for value in self.args_values):
            self.problems.append("training arguments contain a URL")

    def has_url(self, value: object, seen: set[int]) -> bool:
        """True if a string in *value* (recursively) looks like a URL."""
        if isinstance(value, str):
            return bool(_URL_RE.search(value))
        if isinstance(value, _Tuple):
            return any(self.has_url(item, seen) for item in value.items)
        if isinstance(value, _Box) and id(value) not in seen:
            seen.add(id(value))
            return any(
                self.has_url(item, seen)
                for item in [*value.elements, *(v for pair in value.entries for v in pair)]
            )
        return False

    def check_model_yaml(self, value: object) -> None:
        """Reject a model definition Ultralytics would eval() when training from it."""
        if not isinstance(value, _Box) or value.kind != "dict":
            self.problems.append("model yaml is not a plain dict")
            return
        for key, item in value.entries:
            if key == "activation" and item not in _SAFE_ACTIVATIONS:
                self.problems.append("model yaml activation is not a known activation")
            elif key in ("backbone", "head"):
                layers = item.elements if isinstance(item, _Box) and item.kind == "list" else None
                if layers is None:
                    self.problems.append(f"model yaml {key} is not a list")
                    continue
                for layer in layers:
                    spec = layer.elements if isinstance(layer, _Box) else []
                    name = spec[2] if len(spec) >= 3 else None
                    if not _known_layer_name(name):
                        self.problems.append(f"model yaml {key} uses an unknown module")

    def op_PROTO(self, arg: object) -> None:
        self.proto = int(str(arg))

    def op_FRAME(self, _arg: object) -> None:
        pass

    def op_MARK(self, _arg: object) -> None:
        self.marks.append(len(self.stack))

    def op_MEMOIZE(self, _arg: object) -> None:
        self.memo[len(self.memo)] = self.top()

    def op_BINPUT(self, arg: object) -> None:
        self.memo[int(str(arg))] = self.top()

    op_PUT = op_LONG_BINPUT = op_BINPUT

    def op_BINGET(self, arg: object) -> None:
        slot = int(str(arg))
        if slot not in self.memo:
            raise CandidateValidationError("Checkpoint pickle reads an unset memo slot")
        self.stack.append(self.memo[slot])

    op_GET = op_LONG_BINGET = op_BINGET

    def op_GLOBAL(self, arg: object) -> None:
        module, _, attr = str(arg).partition(" ")
        self.found.append((module, attr))
        if module == "__builtin__" and self.proto >= 3:
            # Only protocol < 3 maps __builtin__ to builtins; otherwise
            # Ultralytics answers the import error with a pip install.
            self.problems.append("__builtin__ global in a protocol 3+ pickle")
        self.stack.append(_Global(module, attr))

    def op_STACK_GLOBAL(self, _arg: object) -> None:
        attr, module = self.pop(), self.pop()
        if not isinstance(module, str) or not isinstance(attr, str):
            raise CandidateValidationError("Unresolvable STACK_GLOBAL in checkpoint pickle")
        self.found.append((module, attr))
        self.stack.append(_Global(module, attr))

    def op_EMPTY_TUPLE(self, _arg: object) -> None:
        self.stack.append(_Tuple(()))

    def op_TUPLE1(self, _arg: object) -> None:
        self.stack.append(_Tuple((self.pop(),)))

    def op_TUPLE2(self, _arg: object) -> None:
        second = self.pop()
        self.stack.append(_Tuple((self.pop(), second)))

    def op_TUPLE3(self, _arg: object) -> None:
        third, second = self.pop(), self.pop()
        self.stack.append(_Tuple((self.pop(), second, third)))

    def op_TUPLE(self, _arg: object) -> None:
        self.stack.append(_Tuple(tuple(self.pop_mark())))

    def op_EMPTY_DICT(self, _arg: object) -> None:
        self.stack.append(_Box("dict"))

    def op_DICT(self, _arg: object) -> None:
        box = _Box("dict")
        self.set_items(box, self.pop_mark())
        self.stack.append(box)

    def op_EMPTY_LIST(self, _arg: object) -> None:
        self.stack.append(_Box("list"))

    def op_LIST(self, _arg: object) -> None:
        box = _Box("list")
        items = self.pop_mark()
        self.data(items, "list item")
        self.adopt(box, items)
        box.elements.extend(items)
        self.stack.append(box)

    def op_EMPTY_SET(self, _arg: object) -> None:
        self.stack.append(_Box("set"))

    def op_FROZENSET(self, _arg: object) -> None:
        box = _Box("frozenset")
        items = self.pop_mark()
        self.data(items, "frozenset item")
        self.stack.append(box)

    def op_SETITEM(self, _arg: object) -> None:
        value, key = self.pop(), self.pop()
        self.set_items(self.top(), [key, value])

    def op_SETITEMS(self, _arg: object) -> None:
        pairs = self.pop_mark()
        self.set_items(self.top(), pairs)

    def _extend(self, kind: str, values: list[object], opname: str) -> None:
        box = self.mutate(self.top(), (kind,), opname)
        self.data(values, f"{opname} item")
        if box is not None:
            self.adopt(box, values)
            box.elements.extend(values)

    def op_APPEND(self, _arg: object) -> None:
        self._extend("list", [self.pop()], "APPEND")

    def op_APPENDS(self, _arg: object) -> None:
        self._extend("list", self.pop_mark(), "APPENDS")

    def op_ADDITEMS(self, _arg: object) -> None:
        self._extend("set", self.pop_mark(), "ADDITEMS")

    def op_REDUCE(self, _arg: object) -> None:
        args = self.pop()
        self.stack.append(self.call(self.pop(), args, "REDUCE"))

    def op_NEWOBJ(self, _arg: object) -> None:
        args, cls = self.pop(), self.pop()
        if not _is_layer_class(cls):
            label = f"{cls.module}.{cls.name}" if isinstance(cls, _Global) else "a value"
            self.problems.append(f"NEWOBJ of {label}, which is not an allowed layer class")
        if args != _Tuple(()):
            self.problems.append("NEWOBJ with arguments")
        self.stack.append(_Box("layer"))

    def op_BUILD(self, _arg: object) -> None:
        state = self.pop()
        self.build(self.top(), state)

    def op_BINPERSID(self, _arg: object) -> None:
        self.stack.append(self.persistent(self.pop()))

    def op_POP(self, _arg: object) -> None:
        if self.marks and len(self.stack) == self.marks[-1]:
            self.marks.pop()
        else:
            self.pop()

    def op_POP_MARK(self, _arg: object) -> None:
        self.pop_mark()

    def op_DUP(self, _arg: object) -> None:
        value = self.pop()
        self.stack.extend((value, value))


def audit_pickle(data: bytes) -> tuple[list[tuple[str, str]], list[str]]:
    """Statically walk a pickle stream; return (globals referenced, policy problems).

    Nothing is unpickled.  The walk accepts the shapes ``torch.save`` produces
    for Ultralytics checkpoints and rejects everything else (fail closed):

    * Layer classes come from the generated LAYER_CLASSES list and are only
      NEWOBJ'd without arguments; their state is a plain dict via BUILD.
    * Every call target has an argument check: numeric dtypes only, latin1
      bytes only, argument-free namespaces/OrderedDicts, tensors whose view
      fits their storage, empty backward hooks, ...
    * The single ``getattr`` real checkpoints use (``Detect.forward`` stored
      as ``detect``) may only sit in a dict that becomes BUILD state.
    * Containers are mutated only by the matching opcode and never after they
      were handed to a call or BUILD; dict keys starting with ``__`` and any
      global stored as data (other than torch dtypes and ndarray for numpy's
      _reconstruct) are rejected.
    """
    return _audit(data)[:2]


def _audit(data: bytes) -> tuple[list[tuple[str, str]], list[str], dict[str, tuple[str, int]]]:
    audit = _PickleAudit()
    try:
        audit.run(data)
    except CandidateValidationError:
        raise
    except Exception as exc:
        raise CandidateValidationError(f"Checkpoint pickle is malformed: {exc}") from exc
    return audit.found, audit.problems, audit.storages


def scan_pickle_globals(data: bytes) -> list[tuple[str, str]]:
    """Every (module, name) global a pickle stream references, without unpickling."""
    return audit_pickle(data)[0]


def _check_central_directory(path: Path) -> None:
    """Bound the central directory before ``zipfile.ZipFile`` parses all of it.

    ``ZipFile`` reads the whole central directory into memory and builds a
    ZipInfo per entry, so the entry count and directory size are taken from
    the end-of-central-directory record first (a zip64 record is consulted
    when the classic fields are saturated).
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        window = min(size, 22 + 0xFFFF + 20 + 56 + 4096)  # EOCD, comment, zip64 records
        handle.seek(size - window)
        tail = handle.read(window)
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0 or len(tail) - eocd < 22:
        raise CandidateValidationError("Checkpoint archive has no end-of-central-directory record")
    entries, directory_bytes = struct.unpack_from("<HI", tail, eocd + 10)
    if entries == 0xFFFF or directory_bytes == 0xFFFFFFFF:
        zip64 = tail.rfind(b"PK\x06\x06", 0, eocd)
        if zip64 < 0 or eocd - zip64 < 56:
            raise CandidateValidationError("Checkpoint archive has a truncated zip64 record")
        entries, directory_bytes = struct.unpack_from("<QQ", tail, zip64 + 32)
    if entries > _MAX_ZIP_MEMBERS:
        raise CandidateValidationError(
            f"Checkpoint archive has too many members ({entries} > {_MAX_ZIP_MEMBERS})"
        )
    if directory_bytes > _MAX_ZIP_CENTRAL_DIR_BYTES:
        raise CandidateValidationError("Checkpoint archive central directory is too large")


def _check_archive_bounds(members: list[zipfile.ZipInfo], archive_bytes: int) -> None:
    """Reject archives whose central directory declares more than we will inflate.

    ``testzip`` inflates in bounded chunks and stops at each member's declared
    ``file_size``, so the declared total bounds its work; the pickle itself is
    read with ``_read_member``, which bounds inflation even when the declared
    size lies small.
    """
    if len(members) > _MAX_ZIP_MEMBERS:
        raise CandidateValidationError(
            f"Checkpoint archive has too many members ({len(members)} > {_MAX_ZIP_MEMBERS})"
        )
    declared = sum(max(info.file_size, 0) for info in members)
    allowed = max(_MAX_ZIP_EXPANSION_RATIO * archive_bytes, _ZIP_EXPANSION_FLOOR_BYTES)
    if declared > _MAX_ZIP_UNCOMPRESSED_BYTES or declared > allowed:
        raise CandidateValidationError(
            "Checkpoint archive expands too far: declares "
            f"{declared} uncompressed bytes from a {archive_bytes}-byte file"
        )


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """Read one member, inflating no more than its declared size plus one byte.

    ``ZipFile.read`` asks zlib for up to 1 GiB per call and only afterwards
    truncates to the declared size, so a member whose central-directory size
    (and CRC) lies small could still cost that much memory.  Passing a length
    to ``read`` makes zipfile hand zlib a matching ``max_length``.
    """
    with archive.open(info) as handle:
        data = handle.read(info.file_size + 1)
    if len(data) != info.file_size:
        raise CandidateValidationError(f"Checkpoint member {info.filename!r} is truncated")
    return data


def _validate_torch_zip(path: Path) -> dict[str, Any]:
    with open(path, "rb") as handle:
        magic = handle.read(4)
    # torch.load only takes the zip path when the file *starts* with a local
    # header; anything else is unpickled directly by the legacy loader.
    if magic != b"PK\x03\x04" or not zipfile.is_zipfile(path):
        raise CandidateValidationError(
            "Only PyTorch zip-format .pt checkpoints are accepted (legacy pickle format rejected)"
        )
    _check_central_directory(path)
    archive_bytes = os.stat(path).st_size
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        _check_archive_bounds(members, archive_bytes)
        names = [info.filename for info in members]
        if not members or min(info.header_offset for info in members) != 0:
            raise CandidateValidationError("Checkpoint archive has data before its first member")
        if len(set(names)) != len(names):
            raise CandidateValidationError("Checkpoint archive has duplicate member names")
        pickles = [info for info in members if info.filename.endswith(".pkl")]
        data_pickles = [i for i in pickles if i.filename.split("/")[-1] == "data.pkl"]
        if len(data_pickles) != 1 or len(pickles) != 1:
            raise CandidateValidationError("Checkpoint must contain exactly one pickle, data.pkl")
        info = data_pickles[0]
        if info.file_size > _MAX_PICKLE_BYTES:
            raise CandidateValidationError("Checkpoint pickle is too large")
        # Only now is any member inflated: the bounds above cap what testzip
        # and the pickle read can produce from a small upload.
        corrupt = archive.testzip()
        if corrupt is not None:
            raise CandidateValidationError(f"Checkpoint archive member {corrupt!r} is corrupt")
        found, problems, storages = _audit(_read_member(archive, info))
        rejected = list(problems)
        rejected += [f"{m}.{n}" for m, n in found if not _global_allowed(m, n)]
        # torch sizes each storage from the pickle's numel, not from the record,
        # so a short record would let tensors read and write past its end.
        prefix = info.filename[: -len("data.pkl")]
        sizes = {i.filename: i.file_size for i in members}
        for key, (storage_type, numel) in storages.items():
            expected = numel * _STORAGE_ITEMSIZE[storage_type]
            if sizes.get(f"{prefix}data/{key}") != expected:
                rejected.append(f"storage {key} size does not match its record")
    if rejected:
        raise CandidateValidationError(
            "Checkpoint references disallowed code: " + ", ".join(sorted(set(rejected))[:20])
        )
    return {"format": "torch-zip", "pickle_globals": len(set(found))}


def validate_model_file(path: Path, suffix: str) -> dict[str, Any]:
    """Validate a staged model file without executing or unpickling it."""
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise CandidateValidationError("Model file is missing") from exc
    if not stat.S_ISREG(info.st_mode):
        raise CandidateValidationError("Model file must be a regular file")
    if info.st_size <= 0:
        raise CandidateValidationError("Model file is empty")
    if suffix not in ALLOWED_SUFFIXES:
        raise CandidateValidationError(f"Unsupported model suffix {suffix!r}")
    if suffix == ".pt":
        details = _validate_torch_zip(path)
    elif suffix == ".onnx":
        with open(path, "rb") as handle:
            first = handle.read(1)
        # ONNX ModelProto always starts with field 1 (ir_version, varint).
        if first != b"\x08":
            raise CandidateValidationError("File is not an ONNX ModelProto")
        details = {"format": "onnx", "note": "structural check only"}
    else:
        details = {
            "format": "tensorrt-engine",
            "note": "structural check only; engine contents need the target GPU to verify",
        }
    return {"ok": True, "validated_at": _now(), "size_bytes": info.st_size, **details}


# ── store ───────────────────────────────────────────────────────────────────


def _remove_candidate_dir(directory: Path) -> None:
    """Delete a candidate directory, its manifest last, then the directory."""
    manifest = directory / MANIFEST_NAME
    for entry in directory.iterdir():
        if entry == manifest:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            os.unlink(entry)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(manifest)
    directory.rmdir()


class StagedModel:
    """A model file being written into the store's staging area."""

    def __init__(self, store: ModelStore, suffix: str) -> None:
        if suffix not in ALLOWED_SUFFIXES:
            raise CandidateValidationError(f"Unsupported model suffix {suffix!r}")
        self.store = store
        self.suffix = suffix
        self.id = uuid.uuid4().hex
        self.dir = store.staging_dir / self.id
        self.dir.mkdir(mode=0o755, parents=True)
        self.path = self.dir / f"model{suffix}"
        self._handle: BinaryIO | None = open(self.path, "xb")
        self._digest = hashlib.sha256()
        self.size = 0

    def write(self, chunk: bytes) -> None:
        if self._handle is None:
            raise ModelStoreError("Staged model is closed")
        self._handle.write(chunk)
        self._digest.update(chunk)
        self.size += len(chunk)

    def _close(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
            self._handle = None
            os.chmod(self.path, 0o644)

    def abort(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                self._handle.close()
            self._handle = None
        shutil.rmtree(self.dir, ignore_errors=True)

    def commit(
        self, *, source: str, requested_name: str, provenance: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate, write the manifest and atomically publish the candidate."""
        try:
            self._close()
            validation = validate_model_file(self.path, self.suffix)
            sha256 = self._digest.hexdigest()
            if sha256_file(self.path) != sha256:
                raise ModelStoreError("Staged model changed while being published")
            manifest = {
                "id": self.id,
                "kind": "candidate",
                "source": source,
                "requested_name": requested_name,
                "suffix": self.suffix,
                "file": self.path.name,
                "sha256": sha256,
                "size_bytes": self.size,
                "created_at": _now(),
                "provenance": provenance,
                "validation": validation,
            }
            _write_json_durable(self.dir / MANIFEST_NAME, manifest)
            _fsync_dir(self.dir)
            final = self.store.candidates_dir / self.id
            os.rename(self.dir, final)
            _fsync_dir(self.store.candidates_dir)
            return manifest
        except Exception:
            self.abort()
            raise


class ModelStore:
    """Candidates, rollback copies and history for one MODELS_DIR."""

    def __init__(self, root: Path | str, models_dir: Path | str) -> None:
        self.root = Path(root)
        self.models_dir = Path(models_dir)
        self.staging_dir = self.root / ".staging"
        self.candidates_dir = self.root / "candidates"
        self.rollback_dir = self.root / "rollback"
        self.history_path = self.root / HISTORY_NAME
        for directory in (self.root, self.staging_dir, self.candidates_dir, self.rollback_dir):
            if directory.is_symlink():
                raise ModelStoreError(f"Model store path {directory} must not be a symlink")
            directory.mkdir(mode=0o755, parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, models_dir: Path | str | None = None) -> ModelStore:
        return cls(
            os.environ.get("MODEL_STORE_DIR", "/data/model_store"),
            models_dir or os.environ.get("MODELS_DIR", "/models"),
        )

    # ── publication ──

    def stage(self, suffix: str) -> StagedModel:
        return StagedModel(self, suffix.lower())

    def publish_file(
        self, src: Path, *, source: str, requested_name: str, provenance: dict[str, Any]
    ) -> dict[str, Any]:
        """Copy *src* into a new validated candidate (src itself is untouched)."""
        staged = self.stage(Path(requested_name).suffix)
        try:
            with open(src, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ModelStoreError("Model source must be a regular file")
                while chunk := handle.read(_COPY_CHUNK):
                    staged.write(chunk)
        except Exception:
            staged.abort()
            raise
        return staged.commit(source=source, requested_name=requested_name, provenance=provenance)

    # ── reading ──

    def _load_manifest(self, directory: Path, kind: str) -> dict[str, Any] | None:
        if not _ID_RE.fullmatch(directory.name) or directory.is_symlink():
            return None
        try:
            manifest = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(manifest, dict) or manifest.get("id") != directory.name:
            return None
        if manifest.get("kind") != kind:
            return None
        required = ("sha256", "file", "suffix", "created_at", "provenance")
        if any(not manifest.get(key) for key in required):
            return None
        if not isinstance(manifest["provenance"], dict) or not re.fullmatch(
            r"[0-9a-f]{64}", str(manifest["sha256"])
        ):
            return None
        if kind == "candidate" and not isinstance(manifest.get("validation"), dict):
            return None
        return manifest

    def _list(self, parent: Path, kind: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        try:
            entries = list(parent.iterdir())
        except OSError:
            return items
        for entry in entries:
            manifest = self._load_manifest(entry, kind)
            if manifest is not None:
                items.append(manifest)
        return sorted(items, key=lambda m: str(m.get("created_at", "")), reverse=True)

    def list_candidates(self) -> list[dict[str, Any]]:
        return self._list(self.candidates_dir, "candidate")

    def list_rollbacks(self) -> list[dict[str, Any]]:
        return self._list(self.rollback_dir, "rollback")

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        manifest = (
            self._load_manifest(self.candidates_dir / candidate_id, "candidate")
            if isinstance(candidate_id, str) and _ID_RE.fullmatch(candidate_id)
            else None
        )
        if manifest is None:
            raise ModelStoreError("Candidate not found")
        return manifest

    def get_rollback(self, rollback_id: str) -> dict[str, Any]:
        manifest = (
            self._load_manifest(self.rollback_dir / rollback_id, "rollback")
            if isinstance(rollback_id, str) and _ID_RE.fullmatch(rollback_id)
            else None
        )
        if manifest is None:
            raise ModelStoreError("Rollback copy not found")
        return manifest

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            lines = self.history_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
            if len(records) >= limit:
                break
        return records

    def provenance_index(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Map (file name, sha256) to the newest ledger record that installed it."""
        index: dict[tuple[str, str], dict[str, Any]] = {}
        records = list(reversed(self.history(limit=100_000)))  # oldest first
        # A *_started record only stands for an install if it was not followed
        # by the matching *_failed record (same target and timestamp).
        failed = {
            (r.get("target_name"), r.get("at"))
            for r in records
            if str(r.get("action", "")).endswith("_failed")
        }
        for record in records:
            if record.get("action") not in _INSTALL_ACTIONS:
                continue
            if str(record["action"]).endswith("_started") and (
                record.get("target_name"), record.get("at")
            ) in failed:
                continue
            key = (str(record.get("target_name")), str(record.get("result_sha256")))
            action = _INSTALL_ACTIONS[str(record["action"])]
            index[key] = {
                "status": "recorded",
                "action": action,
                # The artifact whose bytes were installed.  A rollback record's
                # own ``rollback_id`` is the snapshot taken of the file it
                # replaced, so the restored copy is ``restored_rollback_id``.
                "candidate_id": record.get("candidate_id"),
                "rollback_id": (
                    record.get("restored_rollback_id") if action == "rollback" else None
                ),
                "replaced_rollback_id": record.get("rollback_id"),
                "source": record.get("source"),
                "at": record.get("at"),
                "actor": record.get("actor"),
            }
        return index

    def live_provenance(
        self,
        name: str,
        sha256: str,
        index: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Provenance of a live file, or ``unresolved`` when the ledger has no match."""
        lookup = self.provenance_index() if index is None else index
        return lookup.get((name, sha256), {"status": UNRESOLVED})

    # ── promotion / rollback ──

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with open(self.root / ".lock", "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _append_history(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n"
        created = not self.history_path.exists()
        with open(self.history_path, "a+b") as handle:
            if handle.tell() > 0:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    line = "\n" + line  # never glue a record onto a torn last line
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        if created:
            _fsync_dir(self.root)

    def _live_target(self, name: str) -> Path | None:
        target = self.models_dir / name
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise ModelStoreError(f"Live model {name} is not a regular file; refusing to replace it")
        return target

    def _snapshot_live(self, name: str, target: Path, reason: str) -> dict[str, Any]:
        """Copy the current live file into a new rollback slot."""
        rollback_id = uuid.uuid4().hex
        staging = self.staging_dir / rollback_id
        staging.mkdir(mode=0o755)
        try:
            suffix = target.suffix.lower()
            sha256, size = _copy_hashing(target, staging / f"model{suffix}")
            manifest = {
                "id": rollback_id,
                "kind": "rollback",
                "target_name": name,
                "suffix": suffix,
                "file": f"model{suffix}",
                "sha256": sha256,
                "size_bytes": size,
                "created_at": _now(),
                "reason": reason,
                "provenance": self.live_provenance(name, sha256),
            }
            _write_json_durable(staging / MANIFEST_NAME, manifest)
            _fsync_dir(staging)
            os.rename(staging, self.rollback_dir / rollback_id)
            _fsync_dir(self.rollback_dir)
            return manifest
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _install(self, src: Path, expected_sha256: str, name: str) -> None:
        """Atomically place a verified copy of *src* at MODELS_DIR/name.

        Raises only while the live file is still untouched; once ``os.replace``
        has run, a failing directory fsync is logged, not raised, so callers
        never discard the rollback copy of a file that was already replaced.
        """
        temp = self.models_dir / f".{name}.{uuid.uuid4().hex}{_PROMOTE_TMP_SUFFIX}"
        try:
            sha256, _size = _copy_hashing(src, temp)
            if sha256 != expected_sha256:
                raise ModelStoreError("SHA256 mismatch while copying; live model left unchanged")
            os.replace(temp, self.models_dir / name)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()
        try:
            _fsync_dir(self.models_dir)
        except OSError:
            logger.warning("fsync of %s failed after installing %s", self.models_dir, name)

    def _sweep_stale(self) -> None:
        """Remove leftovers of interrupted work. Caller holds the store lock."""
        for leftover in self.models_dir.glob(f".*{_PROMOTE_TMP_SUFFIX}"):
            with contextlib.suppress(OSError):
                if leftover.is_file() and not leftover.is_symlink():
                    leftover.unlink()
        cutoff = time.time() - _STAGING_MAX_AGE_SECONDS
        for entry in self.staging_dir.iterdir():
            with contextlib.suppress(OSError):
                if not entry.is_symlink() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry)

    def _swap(
        self,
        *,
        action: str,
        name: str,
        src: Path,
        sha256: str,
        actor: str,
        reason: str,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        """Snapshot the live file, install *src*, and record it. Caller holds the lock."""
        self._sweep_stale()
        live = self._live_target(name)
        previous = self._snapshot_live(name, live, reason) if live is not None else None
        record = {
            "action": action,
            "at": _now(),
            "actor": actor,
            "target_name": name,
            "result_sha256": sha256,
            "previous_sha256": previous["sha256"] if previous else None,
            "rollback_id": previous["id"] if previous else None,
            **extra,
        }
        try:
            # Write-ahead: if power fails after the replace, the ledger still
            # names what was installed, so provenance is not lost.
            self._append_history({**record, "action": f"{action}_started"})
        except Exception:
            if previous is not None:
                shutil.rmtree(self.rollback_dir / previous["id"], ignore_errors=True)
            raise
        try:
            self._install(src, sha256, name)
        except Exception as exc:
            if previous is not None:
                shutil.rmtree(self.rollback_dir / previous["id"], ignore_errors=True)
            with contextlib.suppress(Exception):
                self._append_history(
                    {**record, "action": f"{action}_failed", "reason": str(exc)[:500]}
                )
            raise
        self._record_or_undo(record, name, previous)
        return record

    def _record_or_undo(
        self, record: dict[str, Any], name: str, previous: dict[str, Any] | None
    ) -> None:
        """Append the final ledger record; if that fails, put the previous file back."""
        try:
            self._append_history(record)
        except Exception as exc:
            if previous is not None:
                try:
                    self._install(
                        self.rollback_dir / previous["id"] / previous["file"],
                        previous["sha256"],
                        name,
                    )
                except Exception as undo_exc:
                    raise ModelStoreError(
                        f"Could not record {record['action']} and could not undo it: {name} is "
                        f"now the new file; rollback copy {previous['id']} was kept"
                    ) from undo_exc
                shutil.rmtree(self.rollback_dir / previous["id"], ignore_errors=True)
            else:
                (self.models_dir / name).unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    _fsync_dir(self.models_dir)
            raise ModelStoreError(f"Could not record {record['action']}; change undone") from exc

    def promote(self, candidate_id: str, target_name: str, *, actor: str) -> dict[str, Any]:
        """Promote a candidate onto MODELS_DIR/target_name, keeping a rollback copy."""
        with self._locked():
            manifest = self.get_candidate(candidate_id)
            suffix = str(manifest["suffix"])
            try:
                name = safe_model_name(target_name, suffix)
            except ValueError as exc:
                raise ModelStoreError(str(exc)) from exc
            model = self.candidates_dir / candidate_id / str(manifest["file"])
            if model.is_symlink() or not model.is_file():
                raise ModelStoreError("Candidate model file is missing")
            if sha256_file(model) != manifest["sha256"]:
                raise ModelStoreError("Candidate SHA256 does not match its manifest")
            try:
                validate_model_file(model, suffix)
            except CandidateValidationError as exc:
                raise ModelStoreError(f"Candidate failed re-validation: {exc}") from exc
            return self._swap(
                action="promote",
                name=name,
                src=model,
                sha256=str(manifest["sha256"]),
                actor=actor,
                reason=f"before promoting candidate {candidate_id}",
                extra={"candidate_id": candidate_id, "source": manifest.get("source")},
            )

    def rollback(self, rollback_id: str, *, actor: str) -> dict[str, Any]:
        """Restore a rollback copy, snapshotting whatever is live first."""
        with self._locked():
            manifest = self.get_rollback(rollback_id)
            try:
                name = safe_model_name(str(manifest.get("target_name", "")), manifest["suffix"])
            except ValueError as exc:
                raise ModelStoreError(str(exc)) from exc
            model = self.rollback_dir / rollback_id / str(manifest["file"])
            if model.is_symlink() or not model.is_file():
                raise ModelStoreError("Rollback model file is missing")
            if sha256_file(model) != manifest["sha256"]:
                raise ModelStoreError("Rollback copy SHA256 does not match its manifest")
            return self._swap(
                action="rollback",
                name=name,
                src=model,
                sha256=str(manifest["sha256"]),
                actor=actor,
                reason=f"before restoring rollback {rollback_id}",
                extra={"restored_rollback_id": rollback_id, "source": "rollback"},
            )

    def discard_candidate(self, candidate_id: str, *, actor: str) -> dict[str, Any]:
        """Delete an unwanted candidate; live models are never affected.

        The ledger record is appended (and fsynced) before any file is removed,
        so a candidate is never gone without an audit record.  If the ledger
        write fails the candidate is untouched and still listed; if the removal
        fails afterwards, a ``discard_failed`` record follows; the manifest is
        removed last, so the candidate stays listed for a retry.
        """
        with self._locked():
            manifest = self.get_candidate(candidate_id)
            record = {
                "action": "discard",
                "at": _now(),
                "actor": actor,
                "candidate_id": candidate_id,
                "sha256": manifest.get("sha256"),
            }
            self._append_history(record)
            try:
                _remove_candidate_dir(self.candidates_dir / candidate_id)
            except OSError as exc:
                with contextlib.suppress(Exception):
                    self._append_history(
                        {**record, "action": "discard_failed", "reason": str(exc)[:500]}
                    )
                raise ModelStoreError(
                    f"Discard was recorded but candidate {candidate_id} could not be removed: "
                    f"{exc}"
                ) from exc
            try:
                _fsync_dir(self.candidates_dir)
            except OSError:
                logger.warning("fsync of %s failed after discarding %s", self.candidates_dir,
                               candidate_id)
            return record
