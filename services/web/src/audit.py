"""Structured audit log for auth + admin state changes.

Records a single row per security-relevant event into the ``audit_events``
table in ``auth.db`` (schema defined in auth.py).  Recording is best-effort:
a failure to write an audit row must never break the underlying request, so
all write paths are wrapped in try/except and fall back to the standard
Python logger.

Typical action strings (lowercase, dotted namespace):
    login.success, login.failure, logout
    config.save, config.tls_upload
    user.create, user.delete, user.role_change, user.password_reset,
    user.disable, user.enable
    api_token.create, api_token.revoke
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from fastapi import Request

_log = logging.getLogger(__name__)

_AUTH_DB_PATH = os.environ.get("AUTH_DB_PATH", "/data/auth.db")


def record(
    db: sqlite3.Connection,
    *,
    action: str,
    user_id: int | None = None,
    username: str | None = None,
    resource: str | None = None,
    client_ip: str | None = None,
    details: str | Mapping[str, Any] | None = None,
) -> None:
    """Insert an audit row. Best-effort — never raises to the caller.

    ``details`` may be a plain string or a mapping; mappings are serialized
    to compact JSON.  Keep details short — this is an index, not a blob
    store.  For large payloads, reference them externally (e.g. a backup
    filename) rather than inlining.

    **Commit semantics:** this function calls ``db.commit()`` so the audit
    row is durable even if the enclosing request handler later errors out.
    Callers that share the connection must be aware that any other
    uncommitted work on that connection will also be committed — hook
    ``record()`` after, not in the middle of, a logical write unit.
    """
    if isinstance(details, Mapping):
        try:
            details_str: str | None = json.dumps(dict(details), separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            details_str = str(details)
    else:
        details_str = details

    try:
        db.execute(
            """INSERT INTO audit_events
                 (ts, user_id, username, action, resource, client_ip, details)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                user_id,
                username,
                action,
                resource,
                client_ip,
                details_str,
            ),
        )
        db.commit()
    except sqlite3.Error as exc:
        _log.warning("audit.record failed for action=%s: %s", action, exc)


def record_request(
    request: "Request",
    *,
    action: str,
    resource: str | None = None,
    details: str | Mapping[str, Any] | None = None,
) -> None:
    """Record an audit event using user+IP pulled from a FastAPI Request.

    Convenience wrapper for admin route handlers that don't already hold
    an open auth.db connection.  Opens and closes its own short-lived
    connection so callers don't need to import ``auth`` just for this.
    Best-effort: swallows all exceptions.
    """
    import auth as auth_module  # local import avoids cycles at module load

    user = getattr(request.state, "user", None) if hasattr(request, "state") else None
    user_id = user.get("user_id") if user else None
    username = user.get("username") if user else None
    client_ip = request.client.host if request.client else None
    try:
        db = auth_module.get_db(_AUTH_DB_PATH)
    except sqlite3.Error as exc:
        _log.warning("audit.record_request could not open auth db for %s: %s", action, exc)
        return
    try:
        record(
            db,
            action=action,
            user_id=user_id,
            username=username,
            resource=resource,
            client_ip=client_ip,
            details=details,
        )
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


def list_events(
    db: sqlite3.Connection,
    *,
    limit: int = 200,
    offset: int = 0,
    action: str | None = None,
    username: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent audit events, newest first.

    Filters are additive: pass ``action`` and/or ``username`` to narrow the
    result.  ``action`` is matched as an LIKE prefix (e.g. ``user.`` returns
    every user.* event).
    """
    where: list[str] = []
    params: list[Any] = []
    if action:
        where.append("action LIKE ?")
        params.append(action + "%")
    if username:
        where.append("username = ?")
        params.append(username)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    params.extend([int(limit), int(offset)])
    rows = db.execute(
        f"""SELECT id, ts, user_id, username, action, resource, client_ip, details
            FROM audit_events{where_sql}
            ORDER BY id DESC
            LIMIT ? OFFSET ?""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]
