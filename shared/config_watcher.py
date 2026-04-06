"""Config file watcher — polls mtime and fires a callback on changes.

Shared module used by both the detector and notifier services.
"""

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 10.0  # seconds between mtime checks


class ConfigWatcher:
    """Background thread that polls *config_path* for mtime changes.

    When a change is detected the YAML is re-parsed and *on_change* is called
    with the new config dict.  Errors in the callback are logged and suppressed
    so the watcher stays alive.
    """

    def __init__(
        self,
        config_path: str,
        on_change: Callable[[dict], None],
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self._path = Path(config_path)
        self._on_change = on_change
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._last_mtime: float = self._current_mtime()
        self._thread = threading.Thread(
            target=self._run,
            name="config-watcher",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        logger.info("Config watcher started (polling every %.0fs)", self._poll_interval)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._poll_interval + 2)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _current_mtime(self) -> float:
        try:
            return os.path.getmtime(self._path)
        except OSError:
            return 0.0

    def _run(self) -> None:
        while not self._stop.wait(timeout=self._poll_interval):
            mtime = self._current_mtime()
            if mtime != self._last_mtime:
                self._last_mtime = mtime
                try:
                    with self._path.open() as f:
                        new_cfg = yaml.safe_load(f)
                    if isinstance(new_cfg, dict):
                        logger.info("Config changed — reloading")
                        self._on_change(new_cfg)
                    else:
                        logger.warning("Config reload skipped — file did not parse to a dict")
                except Exception:
                    logger.exception("Config reload failed — keeping previous config")
