"""Persist system metrics snapshots to SQLite for historical trending."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class MetricsStore:
    """Thread-safe writer for the system_metrics table.

    Opens its own SQLite connection (separate from EventProcessor) so it can
    be called safely from the StatsCollector thread.

    Pruning is handled by RetentionCleaner on the daily retention cycle.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")

    # ── Public API ────────────────────────────────────────────────────────

    def store(self, snapshot: dict) -> None:
        """Write a single metrics sample to the database."""
        timestamp = snapshot.get("timestamp", datetime.now(timezone.utc).isoformat())
        cpu_pct = snapshot.get("cpu_usage_pct")
        gpu_pct = snapshot.get("gpu_usage_pct")
        gpu_temp = snapshot.get("gpu_temp_c")
        ram_used = snapshot.get("ram_used_mb")
        ram_total = snapshot.get("ram_total_mb")

        cameras = snapshot.get("cameras")
        camera_data: str | None = None
        if cameras:
            try:
                camera_data = json.dumps(cameras, default=str)
            except (TypeError, ValueError):
                camera_data = None

        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO system_metrics
                        (timestamp, cpu_pct, gpu_pct, gpu_temp,
                         ram_used_mb, ram_total_mb, camera_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (timestamp, cpu_pct, gpu_pct, gpu_temp,
                     ram_used, ram_total, camera_data),
                )
                self._conn.commit()
            except Exception:
                logger.warning("Failed to store metrics sample", exc_info=True)

