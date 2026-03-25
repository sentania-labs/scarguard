"""ScarGuard auth module — user accounts, sessions, and API tokens.

This module has no FastAPI dependencies so it can be imported standalone
(e.g. from setup.sh via `docker run ... python auth.py create-admin`).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import logging

_log = logging.getLogger(__name__)

AUTH_DB_PATH: str = os.environ.get("AUTH_DB_PATH", "/data/auth.db")

# ── Database ────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL,
    disabled      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    token_hash   TEXT    NOT NULL UNIQUE,
    created_at   TEXT    NOT NULL,
    last_used_at TEXT,
    disabled     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT    NOT NULL,
    ip_address   TEXT,
    success      INTEGER NOT NULL DEFAULT 0,
    attempted_at TEXT    NOT NULL
);
"""


def _connect(db_path: str = AUTH_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str = AUTH_DB_PATH) -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    conn = _connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_db(db_path: str = AUTH_DB_PATH) -> sqlite3.Connection:
    """Return an open connection. Caller is responsible for closing."""
    return _connect(db_path)


def users_exist(db_path: str = AUTH_DB_PATH) -> bool:
    """Return True if at least one non-disabled user exists."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM users WHERE disabled=0 LIMIT 1"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ── Passwords ───────────────────────────────────────────────────────────────

def _prehash(password: str) -> bytes:
    """SHA-256 pre-hash so bcrypt's 72-byte limit never truncates passwords."""
    return hashlib.sha256(password.encode("utf-8")).digest()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("utf-8"))
    except Exception as exc:
        _log.warning("verify_password failed: %s", exc)
        return False


# ── Users ───────────────────────────────────────────────────────────────────

def create_user(
    db: sqlite3.Connection,
    username: str,
    password: str,
    is_admin: bool = True,
) -> int:
    """Create a user and return their id. Raises sqlite3.IntegrityError on duplicate."""
    now = _utcnow()
    cur = db.execute(
        "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?,?,?,?)",
        (username, hash_password(password), int(is_admin), now),
    )
    db.commit()
    return cur.lastrowid  # type: ignore[return-value]


def get_user(db: sqlite3.Connection, username: str) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT id, username, password_hash, is_admin, created_at, disabled FROM users WHERE username=?",
        (username,),
    ).fetchone()
    return dict(row) if row else None


def get_user_by_id(db: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT id, username, password_hash, is_admin, created_at, disabled FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    return dict(row) if row else None


def list_users(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT id, username, is_admin, created_at, disabled FROM users ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def set_user_disabled(db: sqlite3.Connection, user_id: int, disabled: bool) -> None:
    db.execute("UPDATE users SET disabled=? WHERE id=?", (int(disabled), user_id))
    db.commit()


def set_user_password(db: sqlite3.Connection, user_id: int, new_password: str) -> None:
    db.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (hash_password(new_password), user_id),
    )
    db.commit()


def delete_user(db: sqlite3.Connection, user_id: int) -> None:
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()


# ── Sessions ─────────────────────────────────────────────────────────────────

def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def create_session(
    db: sqlite3.Connection,
    user_id: int,
    timeout_hours: int = 24,
) -> str:
    """Create a session and return the raw token (sent in cookie)."""
    raw = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    now = _utcnow()
    expires = _utcnow_plus(hours=timeout_hours)
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at, created_at) VALUES (?,?,?,?)",
        (token_hash, user_id, expires, now),
    )
    db.commit()
    return raw


def validate_session(
    db: sqlite3.Connection, raw_token: str
) -> dict[str, Any] | None:
    """Validate a raw session token. Returns the user dict if valid, else None."""
    token_hash = _hash_token(raw_token)
    now = _utcnow()
    row = db.execute(
        """SELECT u.id AS user_id, u.username, u.is_admin, u.disabled
           FROM sessions s
           JOIN users u ON u.id = s.user_id
           WHERE s.token_hash=? AND s.expires_at > ? AND u.disabled=0""",
        (token_hash, now),
    ).fetchone()
    return dict(row) if row else None


def delete_session(db: sqlite3.Connection, raw_token: str) -> None:
    token_hash = _hash_token(raw_token)
    db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
    db.commit()


def purge_expired_sessions(db: sqlite3.Connection) -> None:
    now = _utcnow()
    db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
    db.commit()


# ── API Tokens ───────────────────────────────────────────────────────────────

def create_api_token(db: sqlite3.Connection, user_id: int, name: str) -> str:
    """Create an API token and return the raw token (shown to user once only)."""
    raw = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    now = _utcnow()
    db.execute(
        "INSERT INTO api_tokens (user_id, name, token_hash, created_at) VALUES (?,?,?,?)",
        (user_id, name, token_hash, now),
    )
    db.commit()
    return raw


def validate_api_token(db: sqlite3.Connection, raw_token: str) -> dict[str, Any] | None:
    """Validate a raw API token. Returns user dict if valid, else None."""
    token_hash = _hash_token(raw_token)
    row = db.execute(
        """SELECT t.id AS token_id, u.id AS user_id, u.username, u.is_admin, u.disabled
           FROM api_tokens t
           JOIN users u ON u.id = t.user_id
           WHERE t.token_hash=? AND t.disabled=0 AND u.disabled=0""",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    touch_api_token(db, token_hash)
    return result


def touch_api_token(db: sqlite3.Connection, token_hash: str) -> None:
    db.execute(
        "UPDATE api_tokens SET last_used_at=? WHERE token_hash=?",
        (_utcnow(), token_hash),
    )
    db.commit()


def list_api_tokens(db: sqlite3.Connection, user_id: int | None = None) -> list[dict[str, Any]]:
    if user_id is not None:
        rows = db.execute(
            "SELECT id, user_id, name, created_at, last_used_at, disabled FROM api_tokens WHERE user_id=? ORDER BY id",
            (user_id,),
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT t.id, t.user_id, u.username, t.name, t.created_at, t.last_used_at, t.disabled
               FROM api_tokens t JOIN users u ON u.id=t.user_id ORDER BY t.id"""
        ).fetchall()
    return [dict(r) for r in rows]


def revoke_api_token(db: sqlite3.Connection, token_id: int) -> None:
    db.execute("UPDATE api_tokens SET disabled=1 WHERE id=?", (token_id,))
    db.commit()


def delete_api_token(db: sqlite3.Connection, token_id: int) -> None:
    db.execute("DELETE FROM api_tokens WHERE id=?", (token_id,))
    db.commit()


# ── Rate limiting / lockout ──────────────────────────────────────────────────

def check_lockout(
    db: sqlite3.Connection,
    username: str,
    ip: str | None,
    max_attempts: int,
    lockout_minutes: int,
) -> bool:
    """Return True if this username is currently locked out."""
    cutoff = _utcnow_minus(minutes=lockout_minutes)
    count = db.execute(
        """SELECT COUNT(*) FROM login_attempts
           WHERE username=? AND success=0 AND attempted_at > ?""",
        (username, cutoff),
    ).fetchone()[0]
    return count >= max_attempts


def record_attempt(
    db: sqlite3.Connection,
    username: str,
    ip: str | None,
    success: bool,
) -> None:
    now = _utcnow()
    db.execute(
        "INSERT INTO login_attempts (username, ip_address, success, attempted_at) VALUES (?,?,?,?)",
        (username, ip, int(success), now),
    )
    # Keep only the last 1000 attempts per username to avoid unbounded growth
    db.execute(
        """DELETE FROM login_attempts WHERE username=? AND id NOT IN (
               SELECT id FROM login_attempts WHERE username=? ORDER BY id DESC LIMIT 1000
           )""",
        (username, username),
    )
    db.commit()


# ── Time helpers ─────────────────────────────────────────────────────────────

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utcnow_plus(hours: int = 0, minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours, minutes=minutes)).isoformat()


def _utcnow_minus(hours: int = 0, minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours, minutes=minutes)).isoformat()


# ── CLI entrypoint (used by setup.sh) ────────────────────────────────────────

def _cli_create_admin(username: str, password: str) -> None:
    if len(password) < 8:
        print("Error: password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)
    db_path = AUTH_DB_PATH
    init_db(db_path)
    db = get_db(db_path)
    try:
        existing = get_user(db, username)
        if existing:
            print(f"Error: user '{username}' already exists.", file=sys.stderr)
            sys.exit(1)
        create_user(db, username, password, is_admin=True)
        print(f"Admin user '{username}' created successfully.")
    except sqlite3.IntegrityError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "create-admin":
        _cli_create_admin(sys.argv[2], sys.argv[3])
    else:
        print("Usage: python auth.py create-admin <username> <password>", file=sys.stderr)
        sys.exit(1)
