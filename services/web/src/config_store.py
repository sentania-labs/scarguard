"""Read and write scarguard.yml with simple file-level locking."""

import os
import threading
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/config/scarguard.yml"))

_lock = threading.Lock()


def load() -> dict:
    with _lock:
        with CONFIG_PATH.open() as f:
            return yaml.safe_load(f)


def save(cfg: dict) -> None:
    with _lock:
        with CONFIG_PATH.open("w") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


def set_armed(armed: bool) -> None:
    cfg = load()
    cfg.setdefault("system", {})["armed"] = armed
    save(cfg)
