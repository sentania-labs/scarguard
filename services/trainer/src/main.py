"""ScarGuard trainer — job consumer loop.

Listens for training job notifications via Redis pub/sub, with a
fallback poll of the training_jobs table every 30 seconds.  Processes
one job at a time.  On startup, marks any stale 'running' jobs as
failed (crash recovery).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import redis
import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/scarguard.yml")
DB_PATH = os.environ.get("DB_PATH", "/data/scarguard.db")
MODELS_DIR = os.environ.get("MODELS_DIR", "/models")
TRAINING_UPLOADS_DIR = os.environ.get("TRAINING_UPLOADS_DIR", "/data/training_uploads")

JOB_NOTIFY_CHANNEL = "scarguard:training:job:notify"
POLL_INTERVAL = 30


def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def _connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _get_redis(cfg: dict) -> redis.Redis:
    redis_cfg = cfg.get("redis", {})
    pw = os.environ.get("REDIS_PASSWORD", "") or None
    return redis.Redis(
        host=redis_cfg.get("host", "redis"),
        port=int(redis_cfg.get("port", 6379)),
        password=pw,
        decode_responses=True,
    )


def _mark_stale_jobs() -> int:
    """Mark any 'running' jobs as failed (crash recovery)."""
    now = datetime.now(timezone.utc).isoformat()
    result_json = json.dumps({"error": "Trainer restarted — job interrupted (possible OOM)"})
    conn = _connect_db()
    try:
        cur = conn.execute(
            "UPDATE training_jobs SET status = 'failed', completed_at = ?, result = ? WHERE status = 'running'",
            (now, result_json),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _get_next_job() -> dict | None:
    """Return the oldest queued job as a dict, or None."""
    conn = _connect_db()
    try:
        row = conn.execute(
            "SELECT * FROM training_jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _set_job_status(job_id: str, status: str, result: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    sets = ["status = ?"]
    params: list[object] = [status]
    if status == "running":
        sets.append("started_at = ?")
        params.append(now)
    if status in ("completed", "failed", "cancelled"):
        sets.append("completed_at = ?")
        params.append(now)
    if result is not None:
        sets.append("result = ?")
        params.append(result)
    params.append(job_id)
    conn = _connect_db()
    try:
        conn.execute(f"UPDATE training_jobs SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
    finally:
        conn.close()


def _process_job(job: dict, cfg: dict, stop_event: threading.Event) -> None:
    """Dispatch a single job to the job runner."""
    from job_runner import run_job

    job_id = job["id"]
    logger.info("Processing job %s (type=%s)", job_id, job["type"])
    _set_job_status(job_id, "running")
    try:
        result = run_job(job, cfg, stop_event)
        _set_job_status(job_id, "completed", result=json.dumps(result))
        logger.info("Job %s completed", job_id)
    except Exception as e:
        logger.exception("Job %s failed", job_id)
        _set_job_status(job_id, "failed", result=json.dumps({"error": str(e)}))


def main() -> None:
    cfg = _load_config()
    log_level = cfg.get("system", {}).get("log_level", "info")
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,
    )
    logger.info("ScarGuard trainer starting")

    Path("/tmp/healthy").touch(exist_ok=True)

    stale = _mark_stale_jobs()
    if stale:
        logger.warning("Marked %d stale running job(s) as failed", stale)

    stop_event = threading.Event()

    def _shutdown(sig: int, _frame: object) -> None:
        logger.info("Received signal %s — shutting down", sig)
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info("Listening for jobs on %s (poll fallback every %ds)", JOB_NOTIFY_CHANNEL, POLL_INTERVAL)

    while not stop_event.is_set():
        try:
            client = _get_redis(cfg)
            pubsub = client.pubsub()
            pubsub.subscribe(JOB_NOTIFY_CHANNEL)

            last_poll = time.monotonic()
            for message in pubsub.listen():
                if stop_event.is_set():
                    break

                Path("/tmp/healthy").touch(exist_ok=True)

                should_check = False
                if message["type"] == "message":
                    should_check = True
                elif time.monotonic() - last_poll >= POLL_INTERVAL:
                    should_check = True
                    last_poll = time.monotonic()

                if should_check:
                    job = _get_next_job()
                    if job:
                        cfg = _load_config()
                        _process_job(job, cfg, stop_event)
                        last_poll = time.monotonic()

            pubsub.unsubscribe()
            client.close()
        except redis.ConnectionError:
            if not stop_event.is_set():
                logger.warning("Redis connection lost — retrying in 5s")
                stop_event.wait(5)
        except Exception:
            logger.exception("Unexpected error in trainer loop")
            stop_event.wait(5)

    logger.info("Trainer stopped cleanly")


if __name__ == "__main__":
    main()
