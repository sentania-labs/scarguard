"""ScarGuard auth module — user accounts, sessions, and API tokens.

This module has no FastAPI dependencies so it can be imported standalone
(e.g. from setup.sh via `docker run ... python auth.py create-admin`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt

_log = logging.getLogger(__name__)

AUTH_DB_PATH: str = os.environ.get("AUTH_DB_PATH", "/data/auth.db")

# ── Roles ───────────────────────────────────────────────────────────────────
#
# Three role tiers as of v0.12.7:
#
#   ROLE_USER   — authenticated; can view dashboard/events/visits/about,
#                 disarm with auto-rearm, label feedback.  No admin pages,
#                 no config editing, no user management.
#   ROLE_VIEWER — "read-only admin".  Can view everything the admin can
#                 (including admin pages and the structured config form),
#                 but with sensitive fields redacted and no write access.
#                 Cannot disarm or modify feedback either.
#   ROLE_ADMIN  — full access, no restrictions.
#
# These are NOT strictly hierarchical: a VIEWER sees more than a USER
# (e.g. admin pages) but writes less (no disarm, no feedback).  Route-level
# authorisation is gated via the helpers in route_auth.py.
#
# The `role` column was added to the users table in v0.12.7; the legacy
# `is_admin` boolean is kept in sync by create_user() / update_user() for
# backwards compatibility and will be dropped in v0.13.x.

ROLE_USER = "user"
ROLE_VIEWER = "viewer"
ROLE_ADMIN = "admin"
VALID_ROLES: frozenset[str] = frozenset({ROLE_USER, ROLE_VIEWER, ROLE_ADMIN})

# ── Database ────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 1,
    role          TEXT    NOT NULL DEFAULT 'user',
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

-- v0.12.8+ structured audit trail for auth + admin state changes.
-- login_attempts stays around for lockout logic; audit_events is the
-- queryable "who did what from where" log surfaced in /admin/audit-log.
CREATE TABLE IF NOT EXISTS audit_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    NOT NULL,
    user_id    INTEGER,
    username   TEXT,
    action     TEXT    NOT NULL,
    resource   TEXT,
    client_ip  TEXT,
    details    TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_events(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action);
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
        _migrate_add_role_column(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate_add_role_column(conn: sqlite3.Connection) -> None:
    """Idempotently add the ``role`` column to an existing users table.

    Fresh databases get the column via the CREATE TABLE in ``_SCHEMA`` and
    this function is a no-op.  For databases created before v0.12.7 the
    column is missing, so we ALTER TABLE and backfill from the legacy
    ``is_admin`` boolean: is_admin=1 → role='admin', is_admin=0 → role='user'.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "role" in cols:
        return
    try:
        conn.execute(
            "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'"
        )
        conn.execute(
            "UPDATE users SET role = CASE WHEN is_admin THEN 'admin' ELSE 'user' END"
        )
        _log.info("Migrated users table: added 'role' column and backfilled from is_admin")
    except sqlite3.Error as exc:
        _log.warning("Failed to add role column to users table: %s", exc)


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
        hash_bytes = password_hash.encode("utf-8")
        if bcrypt.checkpw(_prehash(password), hash_bytes):
            return True
        # Fall back to raw password check for legacy passlib-created hashes
        return bcrypt.checkpw(password.encode("utf-8"), hash_bytes)
    except Exception as exc:
        _log.warning("verify_password failed: %s", exc)
        return False


# ── Users ───────────────────────────────────────────────────────────────────

def create_user(
    db: sqlite3.Connection,
    username: str,
    password: str,
    is_admin: bool = True,
    role: str | None = None,
) -> int:
    """Create a user and return their id. Raises sqlite3.IntegrityError on duplicate.

    ``role`` (v0.12.7+) is the source of truth: 'user', 'viewer', or 'admin'.
    If omitted, it is derived from the legacy ``is_admin`` flag
    (True → 'admin', False → 'user') so existing callers continue to work
    unchanged.  When ``role`` is set, ``is_admin`` is ignored and written to
    the DB as 1 iff role=='admin' (kept in sync for backwards compat).
    """
    if role is None:
        role = ROLE_ADMIN if is_admin else ROLE_USER
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role {role!r}; must be one of {sorted(VALID_ROLES)}")
    is_admin_int = 1 if role == ROLE_ADMIN else 0
    now = _utcnow()
    cur = db.execute(
        "INSERT INTO users (username, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?)",
        (username, hash_password(password), is_admin_int, role, now),
    )
    db.commit()
    return cur.lastrowid  # type: ignore[return-value]


def get_user(db: sqlite3.Connection, username: str) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT id, username, password_hash, is_admin, role, created_at, disabled FROM users WHERE username=?",
        (username,),
    ).fetchone()
    return dict(row) if row else None


def get_user_by_id(db: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT id, username, password_hash, is_admin, role, created_at, disabled FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    return dict(row) if row else None


def list_users(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT id, username, is_admin, role, created_at, disabled FROM users ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def update_user_role(db: sqlite3.Connection, user_id: int, role: str) -> None:
    """Set a user's role. Keeps the legacy ``is_admin`` column in sync."""
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role {role!r}; must be one of {sorted(VALID_ROLES)}")
    is_admin_int = 1 if role == ROLE_ADMIN else 0
    db.execute(
        "UPDATE users SET role=?, is_admin=? WHERE id=?",
        (role, is_admin_int, user_id),
    )
    db.commit()


def try_demote_admin(db: sqlite3.Connection, user_id: int, new_role: str) -> bool:
    """Atomically demote an admin to *new_role*, refusing if they're the last.

    Returns True on success, False if the target is an admin and demoting
    them would leave zero active admins.  The check and the write happen in
    a single UPDATE so two concurrent requests can't both see count=2 and
    both succeed.
    """
    if new_role not in VALID_ROLES:
        raise ValueError(f"invalid role {new_role!r}")
    if new_role == ROLE_ADMIN:
        # Not a demotion; just a normal role update.
        update_user_role(db, user_id, new_role)
        return True
    is_admin_int = 1 if new_role == ROLE_ADMIN else 0
    cur = db.execute(
        """UPDATE users
           SET role=?, is_admin=?
           WHERE id=?
             AND (
               role != ?
               OR (SELECT COUNT(*) FROM users WHERE role=? AND disabled=0) > 1
             )""",
        (new_role, is_admin_int, user_id, ROLE_ADMIN, ROLE_ADMIN),
    )
    db.commit()
    return cur.rowcount > 0


def try_disable_admin(db: sqlite3.Connection, user_id: int, disabled: bool) -> bool:
    """Atomically set disabled=*disabled*, refusing if it would leave zero admins."""
    if not disabled:
        set_user_disabled(db, user_id, False)
        return True
    cur = db.execute(
        """UPDATE users
           SET disabled=1
           WHERE id=?
             AND (
               role != ?
               OR (SELECT COUNT(*) FROM users WHERE role=? AND disabled=0) > 1
             )""",
        (user_id, ROLE_ADMIN, ROLE_ADMIN),
    )
    db.commit()
    return cur.rowcount > 0


def try_delete_admin(db: sqlite3.Connection, user_id: int) -> bool:
    """Atomically delete a user, refusing if it would leave zero active admins."""
    cur = db.execute(
        """DELETE FROM users
           WHERE id=?
             AND (
               role != ?
               OR disabled != 0
               OR (SELECT COUNT(*) FROM users WHERE role=? AND disabled=0) > 1
             )""",
        (user_id, ROLE_ADMIN, ROLE_ADMIN),
    )
    db.commit()
    return cur.rowcount > 0


def count_active_admins(db: sqlite3.Connection) -> int:
    """Return the number of non-disabled users with role='admin'.

    Used to enforce the "cannot delete / demote / disable the last admin"
    rule so a misclick can't lock the operator out of their own instance.
    """
    row = db.execute(
        "SELECT COUNT(*) FROM users WHERE role=? AND disabled=0",
        (ROLE_ADMIN,),
    ).fetchone()
    return int(row[0])


def set_user_disabled(db: sqlite3.Connection, user_id: int, disabled: bool) -> None:
    db.execute("UPDATE users SET disabled=? WHERE id=?", (int(disabled), user_id))
    db.commit()


def set_user_password(db: sqlite3.Connection, user_id: int, new_password: str) -> bool:
    """Set a user's password. Returns True iff a row was updated.

    Returning a bool lets callers (e.g. the audit-log hook in routes/users.py)
    skip logging a "success" when the target user_id no longer exists —
    otherwise a stale id produces a false-positive audit entry.
    """
    cur = db.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (hash_password(new_password), user_id),
    )
    db.commit()
    return cur.rowcount > 0


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
    """Validate a raw session token. Returns the user dict if valid, else None.

    The returned dict carries ``role`` (v0.12.7+) alongside the legacy
    ``is_admin`` boolean.  Callers should prefer ``role`` for new logic;
    ``is_admin`` is retained for backwards compat with older route helpers.
    """
    token_hash = _hash_token(raw_token)
    now = _utcnow()
    row = db.execute(
        """SELECT u.id AS user_id, u.username, u.is_admin, u.role, u.disabled
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
    """Validate a raw API token. Returns user dict if valid, else None.

    The returned dict inherits ``role`` from the owning user, so API tokens
    issued to a viewer are gated at exactly the same level as the viewer's
    session cookies.
    """
    token_hash = _hash_token(raw_token)
    row = db.execute(
        """SELECT t.id AS token_id, u.id AS user_id, u.username, u.is_admin, u.role, u.disabled
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


def revoke_api_token(db: sqlite3.Connection, token_id: int) -> bool:
    """Disable an API token. Returns True iff a row was updated.

    The bool lets audit-log callers avoid logging a false "success" when
    the token_id is stale or already revoked.
    """
    cur = db.execute("UPDATE api_tokens SET disabled=1 WHERE id=?", (token_id,))
    db.commit()
    return cur.rowcount > 0


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
