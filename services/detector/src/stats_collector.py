"""Periodic system and inference stats collector.

Reads CPU/RAM/temperature from /proc and /sys, GPU stats from nvidia-smi or
Jetson sysfs, and per-camera inference metrics from the shared camera_stats
dict.  Writes a JSON snapshot to a Redis key on each cycle so the web service
can poll it.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

import redis as redis_lib

logger = logging.getLogger(__name__)

REDIS_KEY = "scarguard:stats"


class StatsCollector(threading.Thread):
    """Daemon thread that publishes system stats to Redis at a fixed interval."""

    def __init__(
        self,
        redis_cfg: dict,
        interval_seconds: int,
        camera_stats: dict[str, dict],
        camera_stats_lock: threading.Lock,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="stats-collector", daemon=True)
        self._redis_cfg = redis_cfg
        self._interval = max(1, interval_seconds)
        self._camera_stats = camera_stats
        self._camera_stats_lock = camera_stats_lock
        self._stop = stop_event

        # Previous /proc/stat sample for CPU delta calculation
        self._prev_cpu: tuple[float, float] | None = None

        # Detect GPU platform once at init
        self._gpu_platform = self._detect_gpu_platform()
        if self._gpu_platform:
            logger.info("GPU stats platform detected: %s", self._gpu_platform)
        else:
            logger.info("No GPU detected — stats will show CPU-only metrics")

    # ── Platform detection ────────────────────────────────────────────────

    @staticmethod
    def _detect_gpu_platform() -> str | None:
        """Return 'tegrastats', 'jetson', 'nvidia-smi', or None."""
        # tegrastats: official Jetson tool, covers all JetPack platforms incl. Orin
        if shutil.which("tegrastats"):
            return "tegrastats"
        # Old Jetson sysfs path (pre-Orin boards: Nano, TX2, Xavier)
        if Path("/sys/devices/gpu.0/load").exists():
            return "jetson"
        # x86/generic: check for nvidia-smi binary
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return "nvidia-smi"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None

    # ── CPU stats from /proc/stat ─────────────────────────────────────────

    def _read_cpu_usage(self) -> float:
        """Return CPU usage percentage since last call (0-100)."""
        try:
            with open("/proc/stat") as f:
                line = f.readline()  # first line is aggregate "cpu ..."
            parts = line.split()
            # user, nice, system, idle, iowait, irq, softirq, steal
            values = [float(v) for v in parts[1:9]]
            idle = values[3] + values[4]  # idle + iowait
            total = sum(values)

            if self._prev_cpu is None:
                self._prev_cpu = (idle, total)
                return 0.0

            prev_idle, prev_total = self._prev_cpu
            self._prev_cpu = (idle, total)

            d_idle = idle - prev_idle
            d_total = total - prev_total
            if d_total == 0:
                return 0.0
            return round((1.0 - d_idle / d_total) * 100.0, 1)
        except Exception:
            logger.debug("Failed to read /proc/stat", exc_info=True)
            return 0.0

    # ── RAM from /proc/meminfo ────────────────────────────────────────────

    @staticmethod
    def _read_ram() -> tuple[int, int]:
        """Return (used_mb, total_mb)."""
        try:
            info: dict[str, int] = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split()
                    key = parts[0].rstrip(":")
                    if key in ("MemTotal", "MemAvailable"):
                        info[key] = int(parts[1])  # in kB
                    if len(info) == 2:
                        break
            total_mb = info.get("MemTotal", 0) // 1024
            avail_mb = info.get("MemAvailable", 0) // 1024
            return (total_mb - avail_mb, total_mb)
        except Exception:
            logger.debug("Failed to read /proc/meminfo", exc_info=True)
            return (0, 0)

    # ── Temperature from /sys/class/thermal ───────────────────────────────

    @staticmethod
    def _read_thermal(type_match: str | None = None) -> float | None:
        """Read temperature from thermal zones, optionally matching a type string.

        Returns degrees Celsius or None if not found.
        """
        thermal_base = Path("/sys/class/thermal")
        if not thermal_base.exists():
            return None

        try:
            for zone in sorted(thermal_base.glob("thermal_zone*")):
                if type_match:
                    type_file = zone / "type"
                    if type_file.exists():
                        zone_type = type_file.read_text().strip()
                        if type_match.lower() not in zone_type.lower():
                            continue
                temp_file = zone / "temp"
                if temp_file.exists():
                    raw = int(temp_file.read_text().strip())
                    return round(raw / 1000.0, 1)  # millidegrees → degrees
        except Exception:
            logger.debug("Failed to read thermal zone", exc_info=True)
        return None

    # ── GPU stats ─────────────────────────────────────────────────────────

    def _read_gpu_stats(self) -> dict:
        """Return GPU stats dict.  Keys present only if data is available."""
        if self._gpu_platform == "tegrastats":
            return self._read_gpu_tegrastats()
        elif self._gpu_platform == "jetson":
            return self._read_gpu_jetson()
        elif self._gpu_platform == "nvidia-smi":
            return self._read_gpu_nvidia_smi()
        return {}

    def _read_gpu_tegrastats(self) -> dict:
        """Read GPU stats via tegrastats (all Jetson platforms including Orin).

        Spawns tegrastats, reads one output line, then terminates it.
        Parses GR3D_FREQ for GPU load % and GPU@XXC for temperature.
        """
        try:
            proc = subprocess.Popen(
                ["tegrastats", "--interval", "100"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            line = ""
            try:
                if proc.stdout is None:
                    return {}
                line = proc.stdout.readline()
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        except (FileNotFoundError, OSError):
            return {}
        except Exception:
            logger.debug("tegrastats read failed", exc_info=True)
            return {}

        if not line:
            return {}

        stats: dict = {"gpu_available": True}

        # GPU load: "GR3D_FREQ 45%@612" or "GR3D_FREQ 45%"
        m = re.search(r"GR3D_FREQ\s+(\d+)%", line)
        if m:
            stats["gpu_usage_pct"] = float(m.group(1))

        # GPU temp: "GPU@51C" or "GPU@50.5C"
        m = re.search(r"\bGPU@([\d.]+)C\b", line)
        if m:
            stats["gpu_temp_c"] = float(m.group(1))

        return stats

    @staticmethod
    def _read_gpu_jetson() -> dict:
        """Read Jetson GPU stats from sysfs."""
        stats: dict = {"gpu_available": True}
        try:
            # GPU load: 0-1000 scale (divide by 10 for percentage)
            load_path = Path("/sys/devices/gpu.0/load")
            if load_path.exists():
                raw = int(load_path.read_text().strip())
                stats["gpu_usage_pct"] = round(raw / 10.0, 1)
        except Exception:
            logger.debug("Failed to read Jetson GPU load", exc_info=True)

        # GPU temperature — look for a zone named GPU-therm or similar
        gpu_temp = StatsCollector._read_thermal("gpu")
        if gpu_temp is not None:
            stats["gpu_temp_c"] = gpu_temp

        # GPU memory — try nvidia-smi first (available on JetPack 6), else skip
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split(",")
                if len(parts) == 2:
                    stats["gpu_mem_used_mb"] = int(parts[0].strip())
                    stats["gpu_mem_total_mb"] = int(parts[1].strip())
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        return stats

    @staticmethod
    def _read_gpu_nvidia_smi() -> dict:
        """Read GPU stats via nvidia-smi (x86 or JetPack with nvidia-smi)."""
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return {}

            lines = result.stdout.strip().splitlines()
            if not lines:
                return {}
            parts = lines[0].split(",")  # first GPU only
            if len(parts) < 4:
                return {}

            return {
                "gpu_available": True,
                "gpu_usage_pct": round(float(parts[0].strip()), 1),
                "gpu_mem_used_mb": int(parts[1].strip()),
                "gpu_mem_total_mb": int(parts[2].strip()),
                "gpu_temp_c": round(float(parts[3].strip()), 1),
            }
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return {}
        except Exception:
            logger.debug("Failed to read nvidia-smi", exc_info=True)
            return {}

    # ── Collect all stats ─────────────────────────────────────────────────

    def _collect(self) -> dict:
        """Build the full stats snapshot."""
        cpu_pct = self._read_cpu_usage()
        ram_used, ram_total = self._read_ram()
        cpu_temp = self._read_thermal("cpu")

        snapshot: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cpu_usage_pct": cpu_pct,
            "ram_used_mb": ram_used,
            "ram_total_mb": ram_total,
            "cpu_temp_c": cpu_temp,
            "gpu_available": False,
        }

        gpu = self._read_gpu_stats()
        if gpu:
            snapshot.update(gpu)

        # Per-camera inference stats (deep copy — camera threads replace inner dicts atomically)
        with self._camera_stats_lock:
            snapshot["cameras"] = {k: dict(v) for k, v in self._camera_stats.items()}

        return snapshot

    # ── Thread run loop ───────────────────────────────────────────────────

    def run(self) -> None:
        logger.info(
            "StatsCollector started (interval=%ds, gpu=%s)",
            self._interval,
            self._gpu_platform or "none",
        )

        # Redis client is lazy — actual connection happens on first command.
        client = redis_lib.Redis(
            host=self._redis_cfg.get("host", "redis"),
            port=int(self._redis_cfg.get("port", 6379)),
            decode_responses=True,
            socket_connect_timeout=5,
            retry_on_timeout=True,
        )

        # First collection primes the CPU delta; discard the result.
        self._read_cpu_usage()

        while not self._stop.wait(self._interval):
            try:
                snapshot = self._collect()
                client.set(
                    REDIS_KEY,
                    json.dumps(snapshot, default=str),
                    ex=self._interval * 3,
                )
            except redis_lib.RedisError:
                logger.warning("StatsCollector failed to write to Redis", exc_info=True)
            except Exception:
                logger.exception("StatsCollector collection error")

        logger.info("StatsCollector stopped")
