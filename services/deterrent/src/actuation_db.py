"""SQLite persistence for actuation events.

The deterrent service is the sole writer.  The web service reads this DB
read-only for the actuation log page.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sqlite3
import threading

from actuation_models import ActuationEvent

logger = logging.getLogger(__name__)

DB_PATH: str = os.environ.get("DETERRENT_DB_PATH", "/data/deterrent.db")

# ---------------------------------------------------------------------------
# HMAC key for tamper-evident hash chain on actuation_events rows.
# Loaded lazily so the module can import without side-effects.
# ---------------------------------------------------------------------------
_hmac_key: bytes | None = None
_hmac_key_loaded: bool = False
_hmac_key_warned: bool = False


def _get_hmac_key() -> bytes | None:
    """Return the HMAC key, loading from the environment on first call."""
    global _hmac_key, _hmac_key_loaded
    if not _hmac_key_loaded:
        from event_signing import load_key_from_env
        _hmac_key = load_key_from_env()
        _hmac_key_loaded = True
    return _hmac_key


def _compute_row_hash(
    key: bytes,
    prev_hash: str,
    timestamp: str,
    trigger_class: str,
    trigger_camera: str,
    event_type: str,
    request_id: str,
) -> str:
    """Compute HMAC-SHA256 over the canonical pipe-delimited row fields."""
    canonical = "|".join([
        prev_hash,
        timestamp,
        trigger_class,
        trigger_camera,
        event_type,
        request_id,
    ])
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()

_lock = threading.Lock()
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Return a thread-local connection (created on first use per thread)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def init_db() -> None:
    """Create tables and indexes if they don't exist.  v0.13.3 adds three
    latency columns + group_name — all additive, migrated with ALTER on
    pre-existing databases."""
    with _lock:
        conn = _get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS actuation_events (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp           TEXT    NOT NULL,
                trigger_class       TEXT    NOT NULL,
                trigger_camera      TEXT    NOT NULL,
                trigger_confidence  REAL    NOT NULL,
                pre_delay_sec       REAL    NOT NULL,
                total_duration_sec  REAL    NOT NULL,
                device_count        INTEGER NOT NULL,
                success_count       INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_actuation_ts
                ON actuation_events(timestamp);

            CREATE TABLE IF NOT EXISTS device_actions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id        INTEGER NOT NULL REFERENCES actuation_events(id),
                device_name     TEXT    NOT NULL,
                device_id       TEXT    NOT NULL,
                device_type     TEXT    NOT NULL,
                duration_sec    REAL    NOT NULL,
                delay_before_sec REAL   NOT NULL,
                success         INTEGER NOT NULL DEFAULT 0,
                error           TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_actions_event
                ON device_actions(event_id);
        """)
        # v0.13.3 additive columns — safe to re-run on existing DBs.
        _add_column_if_missing(conn, "actuation_events", "group_name", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "actuation_events", "trigger_delay_ms", "REAL")
        _add_column_if_missing(conn, "actuation_events", "queue_depth", "INTEGER")
        _add_column_if_missing(conn, "device_actions", "cloud_ack_ms", "REAL")
        # v1.14 additive columns — request_id for trace correlation,
        # event_type to distinguish detection / test_fire / force_off /
        # reconcile, off_attempts to surface OFF retry pressure.
        _add_column_if_missing(
            conn, "actuation_events", "request_id", "TEXT NOT NULL DEFAULT ''",
        )
        _add_column_if_missing(
            conn, "actuation_events", "event_type",
            "TEXT NOT NULL DEFAULT 'detection'",
        )
        _add_column_if_missing(
            conn, "device_actions", "off_attempts", "INTEGER NOT NULL DEFAULT 1",
        )
        _add_column_if_missing(
            conn, "device_actions", "stuck", "INTEGER NOT NULL DEFAULT 0",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_actuation_request_id "
            "ON actuation_events(request_id)",
        )
        # v1.14.4: tamper-evident HMAC hash chain columns.
        _add_column_if_missing(
            conn, "actuation_events", "prev_hash", "TEXT NOT NULL DEFAULT ''",
        )
        _add_column_if_missing(
            conn, "actuation_events", "row_hash", "TEXT NOT NULL DEFAULT ''",
        )
        conn.commit()
        logger.info("Actuation database initialised at %s", DB_PATH)


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, type_clause: str,
) -> None:
    """Add *column* of *type_clause* to *table* if it doesn't already exist."""
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in cols:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_clause}")
    logger.info("Added column %s.%s", table, column)


def insert_event(event: ActuationEvent) -> int:
    """Persist an actuation event and its device actions.  Returns the row ID."""
    global _hmac_key_warned
    with _lock:
        conn = _get_conn()

        # --- hash chain: compute prev_hash + row_hash -----------------
        key = _get_hmac_key()
        prev_hash = ""
        row_hash = ""
        if key is not None:
            row = conn.execute(
                "SELECT row_hash FROM actuation_events ORDER BY id DESC LIMIT 1",
            ).fetchone()
            prev_hash = row["row_hash"] if row is not None else ""
            row_hash = _compute_row_hash(
                key,
                prev_hash,
                event.timestamp,
                event.trigger_class,
                event.trigger_camera,
                event.event_type,
                event.request_id,
            )
        elif not _hmac_key_warned:
            logger.warning(
                "DETECTION_HMAC_KEY not set — actuation hash chain disabled",
            )
            _hmac_key_warned = True

        cur = conn.execute(
            """INSERT INTO actuation_events
               (timestamp, trigger_class, trigger_camera, trigger_confidence,
                pre_delay_sec, total_duration_sec, device_count, success_count,
                group_name, trigger_delay_ms, queue_depth,
                request_id, event_type,
                prev_hash, row_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.timestamp,
                event.trigger_class,
                event.trigger_camera,
                event.trigger_confidence,
                event.pre_delay_sec,
                event.total_duration_sec,
                len(event.actions),
                sum(1 for a in event.actions if a.success),
                event.group_name,
                event.trigger_delay_ms,
                event.queue_depth,
                event.request_id,
                event.event_type,
                prev_hash,
                row_hash,
            ),
        )
        event_id = cur.lastrowid
        if event_id is None:
            raise RuntimeError("Failed to get lastrowid after INSERT")
        for action in event.actions:
            conn.execute(
                """INSERT INTO device_actions
                   (event_id, device_name, device_id, device_type,
                    duration_sec, delay_before_sec, success, error, cloud_ack_ms,
                    off_attempts, stuck)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    action.device_name,
                    action.device_id,
                    action.device_type,
                    action.duration_sec,
                    action.delay_before_sec,
                    int(action.success),
                    action.error,
                    action.cloud_ack_ms,
                    action.off_attempts,
                    int(action.stuck),
                ),
            )
        conn.commit()
        logger.debug("Actuation event %d persisted (%d actions)", event_id, len(event.actions))
        return event_id


def verify_chain() -> tuple[bool, str]:
    """Walk the actuation_events table and verify every HMAC hash link.

    Returns ``(True, "N rows verified")`` when the chain is intact, or
    ``(False, "<reason>")`` on the first broken link or missing key.
    """
    key = _get_hmac_key()
    if key is None:
        return (False, "HMAC key not available")

    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT id, timestamp, trigger_class, trigger_camera, "
            "event_type, request_id, prev_hash, row_hash "
            "FROM actuation_events ORDER BY id",
        ).fetchall()

    expected_prev = ""
    for row in rows:
        row_id: int = row["id"]
        stored_prev: str = row["prev_hash"]
        stored_hash: str = row["row_hash"]

        # Validate prev_hash links to previous row's row_hash
        if stored_prev != expected_prev:
            return (
                False,
                f"Chain broken at row ID {row_id}: "
                f"prev_hash mismatch (expected {expected_prev!r}, "
                f"got {stored_prev!r})",
            )

        expected_hash = _compute_row_hash(
            key,
            stored_prev,
            row["timestamp"],
            row["trigger_class"],
            row["trigger_camera"],
            row["event_type"],
            row["request_id"],
        )
        if not hmac.compare_digest(stored_hash, expected_hash):
            return (
                False,
                f"Chain broken at row ID {row_id}: row_hash mismatch",
            )

        expected_prev = stored_hash

    return (True, f"{len(rows)} rows verified")
