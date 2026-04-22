"""Shared periodic healthcheck heartbeat.

The detector / notifier / deterrent services touch ``/tmp/healthy`` and
the Docker healthcheck verifies it was touched within the last minute
(``find /tmp/healthy -mmin -1``). Pre-v1.14 the file was only touched
on incoming messages — long quiet periods would flip the container to
unhealthy unjustly. This module spins a background thread that touches
the file on a fixed interval regardless of traffic.

The thread is a daemon — no clean shutdown needed; it dies with the
process.
"""

from __future__ import annotations

import logging
import pathlib
import threading
import time

logger = logging.getLogger(__name__)

DEFAULT_PATH = pathlib.Path("/tmp/healthy")
DEFAULT_INTERVAL = 15.0


def start_heartbeat(
    path: pathlib.Path | str = DEFAULT_PATH,
    interval_seconds: float = DEFAULT_INTERVAL,
) -> threading.Thread:
    """Start a daemon thread that touches *path* every *interval_seconds*.

    Returns the thread for visibility / testing. Caller does not need to
    keep a reference."""
    target_path = pathlib.Path(path)

    def _run() -> None:
        # Touch immediately so the file exists before the first healthcheck
        # interval elapses.
        try:
            target_path.touch(exist_ok=True)
        except Exception as exc:
            logger.warning("Initial healthcheck touch failed: %s", exc)
        while True:
            time.sleep(interval_seconds)
            try:
                target_path.touch(exist_ok=True)
            except Exception as exc:
                logger.warning("Healthcheck touch failed: %s", exc)

    t = threading.Thread(target=_run, name="healthcheck-heartbeat", daemon=True)
    t.start()
    return t
