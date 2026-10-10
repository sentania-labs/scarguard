"""Redis ACL user initialisation helpers.

Creates named ACL users at Redis startup using ``redis-cli ACL`` commands
sent from a supervisor script.  The supervisor connects with the admin
password (the legacy ``REDIS_PASSWORD``) and creates one named user per
service with the minimum required commands, key patterns, and pub/sub
channels.

Each user's password is generated once and written to an ACL file that
``redis-server`` loads on every start.  If the file already exists the
users are *not* recreated - this makes the migration idempotent.

Service permissions:

+-------------------+----------------------------------------------+
| Service           | Commands / Keys / Channels                   |
+===================+==============================================+
| detector          | PUBLISH scarguard:detections                 |
|                   | GET scarguard:detector:state                 |
|                   | SET scarguard:detector:state                 |
|                   | SUBSCRIBE scarguard:detector:command         |
|                   | PUBLISH scarguard:detector:command           |
|                   | SUBSCRIBE scarguard:trainer:heartbeat        |
+-------------------+----------------------------------------------+
| notifier          | SUBSCRIBE scarguard:detections               |
|                   | SUBSCRIBE scarguard:health                   |
|                   | GET scarguard:health                         |
+-------------------+----------------------------------------------+
| deterrent         | SUBSCRIBE scarguard:detections               |
|                   | SUBSCRIBE scarguard:actuations               |
|                   | SUBSCRIBE scarguard:metrics:drops            |
|                   | GET/PUBLISH/DEL scarguard:off-watchdog:lease*|
|                   | EVAL ... (Lua scripts for leases)            |
+-------------------+----------------------------------------------+
| off-watchdog      | GET/PUBLISH/DOL/SCAN scarguard:off-watchdog* |
|                   | EVAL ... (Lua for lease enforcement)         |
+-------------------+----------------------------------------------+
| web               | SUBSCRIBE scarguard:detections               |
|                   | GET/PSET/PSETEX/INCR scarguard:rl*           |
|                   | GET/PUBLISH scarguard:logs:*                 |
|                   | GET/PUBLISH scarguard:backup:status          |
|                   | GET/PUBLISH scarguard:backup:trigger         |
+-------------------+----------------------------------------------+
| backup            | SUBSCRIBE scarguard:backup:trigger           |
|                   | PUBLISH scarguard:backup:status              |
+-------------------+----------------------------------------------+
| log-streamer      | PUBLISH scarguard:logs:*                     |
|                   | LRANGE/LPUSH/LTRIM/DEL scarguard:logs:buffer:*|
|                   | ZADD/ZREMRANGEBYSCORE/ZCARD scarguard:logs:* |
|                   | GET/SET scarguard:logs:health                |
|                   | EVAL ... (Lua health scripts)                |
+-------------------+----------------------------------------------+
| training-controller | SUBSCRIBE scarguard:detector:state         |
|                     | GET/PUBLISH/DOL scarguard:trainer:*         |
+-------------------+----------------------------------------------+
| trainer             | SUBSCRIBE scarguard:detector:command        |
|                     | SET/PUBLISH scarguard:trainer:heartbeat     |
|                     | SUBSCRIBE scarguard:detector:state           |
+-------------------+----------------------------------------------+

All services must also be allowed PING for health checks.

When no ACL file is present (old deployments), this module falls back
to the legacy admin-password model so existing installs continue to work.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ── ACL file path ────────────────────────────────────────────────────────────
# Written to /data so it survives across container restarts but lives inside
# the redis-data volume (mounted at /data in the Redis container).
ACL_FILE_PATH = "/data/scarguard-acl.txt"

# ── Service permissions ──────────────────────────────────────────────────────

# Format: (username, ["COMMAND", ...], ["KEY_PATTERN", ...], ["CHANNEL", ...])
# Channels are pub/sub targets: SUBSCRIBE for incoming, PUBLISH for outgoing.

_SERVICE_ACL: list[tuple[str, list[str], list[str], list[str]]] = [
    # ── detector ──────────────────────────────────────────────────────────
    (
        "detector",
        [
            "PUBLISH",
            "GET",
            "SET",
            "SUBSCRIBE",
            "PING",
        ],
        [
            "scarguard:detector:state",
        ],
        [
            "scarguard:detections",          # PUBLISH detection events
            "scarguard:detector:command",    # SUBSCRIBE pause/resume commands
        ],
    ),
    # ── notifier ─────────────────────────────────────────────────────────
    (
        "notifier",
        [
            "SUBSCRIBE",
            "GET",
            "PING",
        ],
        [
            "scarguard:health",
        ],
        [
            "scarguard:detections",          # SUBSCRIBE detection events
            "scarguard:health",              # SUBSCRIBE health alerts
        ],
    ),
    # ── deterrent ────────────────────────────────────────────────────────
    (
        "deterrent",
        [
            "SUBSCRIBE",
            "PUBLISH",
            "GET",
            "SET",
            "DEL",
            "EVAL",
            "PING",
        ],
        [
            "scarguard:off-watchdog:lease:*",
        ],
        [
            "scarguard:detections",          # SUBSCRIBE detections
            "scarguard:actuations",           # SUBSCRIBE actuation events
            "scarguard:metrics:drops",        # SUBSCRIBE metric drops
        ],
    ),
    # ── off-watchdog ─────────────────────────────────────────────────────
    (
        "off-watchdog",
        [
            "GET",
            "DEL",
            "SCAN",
            "EVAL",
            "PING",
        ],
        [
            "scarguard:off-watchdog:lease:*",
            "scarguard:off-watchdog:lease:*:deadline",
        ],
        [],  # off-watchdog does not pub/sub; it polls keys
    ),
    # ── web ──────────────────────────────────────────────────────────────
    (
        "web",
        [
            "SUBSCRIBE",
            "PUBLISH",
            "GET",
            "INCR",
            "PSETEX",
            "TTL",
            "EXPIRE",
            "PING",
        ],
        [
            "scarguard:rl:*",
            "scarguard:logs:*",
            "scarguard:backup:status",
            "scarguard:backup:trigger",
        ],
        [
            "scarguard:detections",          # SUBSCRIBE for SSE stream
        ],
    ),
    # ── backup ───────────────────────────────────────────────────────────
    (
        "backup",
        [
            "SUBSCRIBE",
            "PUBLISH",
            "PING",
        ],
        [],  # no key access needed for trigger/status channels
        [
            "scarguard:backup:trigger",       # SUBSCRIBE manual triggers
        ],
    ),
    # ── log-streamer ─────────────────────────────────────────────────────
    (
        "log-streamer",
        [
            "PUBLISH",
            "LPUSH",
            "LRANGE",
            "LTRIM",
            "DEL",
            "ZADD",
            "ZREMRANGEBYSCORE",
            "ZCARD",
            "SET",
            "EVAL",
            "PING",
        ],
        [
            "scarguard:logs:buffer:*",
            "scarguard:logs:health",
        ],
        [
            "scarguard:logs:*",               # PUBLISH log lines
        ],
    ),
    # ── training-controller ──────────────────────────────────────────────
    (
        "training-controller",
        [
            "SUBSCRIBE",
            "GET",
            "SET",
            "DEL",
            "PING",
        ],
        [
            "scarguard:trainer:*",
        ],
        [
            "scarguard:detector:state",       # SUBSCRIBE detector state
        ],
    ),
    # ── trainer ──────────────────────────────────────────────────────────
    (
        "trainer",
        [
            "SUBSCRIBE",
            "SET",
            "GET",
            "PUBLISH",
            "PING",
        ],
        [
            "scarguard:trainer:heartbeat",
            "scarguard:detector:state",
        ],
        [
            "scarguard:detector:command",     # SUBSCRIBE pause/resume
        ],
    ),
]


def _format_userline(user: str, cmds: list[str], keys: list[str], channels: list[str]) -> str:
    """Convert a service permission tuple to a Redis ACL USER line."""
    parts = [f"user {user} on"]
    parts.extend(cmds)
    for key_pat in keys:
        parts.append(f"&{key_pat}")
    for ch in channels:
        parts.append(f"&{ch}")
    return " ".join(parts)


def _generate_acl_content(admin_password: str) -> str:
    """Return the full ACL file content as a string.

    The first line is ALWAYS ``user default ...`` to ensure the default
    user retains access.  This is important so that Redis health-check
    commands (which may use the default user) continue to work.
    """
    lines: list[str] = [
        "# Auto-generated by Scarguard Redis ACL migration",
        "# See shared/acl_init.py for documentation.",
        "",
        f"user default on >{admin_password} ~* +@all",
        "",
    ]
    for username, cmds, keys, channels in _SERVICE_ACL:
        for _ in range(3):  # Placeholder: actual passwords injected later
            pass
    # Generate user lines with placeholder passwords (filled in by generate_user_passwords)
    content_lines: list[str] = lines[:]
    for username, cmds, keys, channels in _SERVICE_ACL:
        cmd_str = " ".join(cmds)
        key_str = " ".join(f"&{k}" for k in keys)
        ch_str = " ".join(f"&{k}" for k in channels)
        content_lines.append(f"user {username} on {cmd_str} {key_str} {ch_str}")
        content_lines.append("")
    return "\n".join(content_lines)


def generate_user_passwords(admin_password: str) -> dict[str, str]:
    """Generate per-service passwords and return a mapping.

    The passwords are stored alongside the ACL file so that each service
    can read only its own password.

    Returns a dict mapping username -> password.
    """
    passwords: dict[str, str] = {}
    for username, _, _, _ in _SERVICE_ACL:
        passwords[username] = base64.b64encode(os.urandom(32)).decode("ascii")

    # Write the ACL file with actual passwords
    acl_path = Path(ACL_FILE_PATH)
    content = _generate_acl_content(admin_password)

    # Replace the user lines with password-protected versions
    output_lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("user ") and stripped != content_lines[0] if (content_lines := content.splitlines()) else False:
            for username, cmds, keys, channels in _SERVICE_ACL:
                userline_prefix = f"user {username} on"
                if stripped.startswith(userline_prefix):
                    pw = passwords.get(username, admin_password)
                    pw_parts = [userline_prefix, f">{pw}"]
                    # Add key/channel constraints
                    for key_pat in keys:
                        pw_parts.append(f"&{key_pat}")
                    for ch in channels:
                        pw_parts.append(f"&{ch}")
                    output_lines.append(" ".join(pw_parts))
                    break
            else:
                output_lines.append(line)
        else:
            output_lines.append(line)

    acl_path.write_text("\n".join(output_lines) + "\n")
    acl_path.chmod(0o600)

    return passwords


def try_init_acl(admin_password: str) -> dict[str, str] | None:
    """Initialise the ACL file if it does not already exist.

    Returns the password map if a new file was created, ``None`` if
    the file already existed (no-op), or ``None`` if Redis is
    unavailable (best-effort).

    On old deployments without an ACL file, we generate one and attempt
    to load it into Redis.  The admin user must be able to write
    ``/data/scarguard-acl.txt``.
    """
    acl_path = Path(ACL_FILE_PATH)

    if acl_path.exists():
        logger.info("Redis ACL file already exists at %s - skipping generation", ACL_FILE_PATH)
        return None

    logger.info("Generating Redis ACL file at %s", ACL_FILE_PATH)
    passwords = generate_user_passwords(admin_password)

    # Verify the file was written correctly
    try:
        lines = acl_path.read_text().splitlines()
        logger.info("Generated ACL file with %d lines", len(lines))
    except OSError as exc:
        logger.error("Failed to read back generated ACL file: %s", exc)
        return None

    return passwords


def get_service_password(username: str) -> str | None:
    """Read a specific service's password from the ACL file.

    Returns ``None`` if the ACL file does not exist or the user is
    not found, so services gracefully fall back to admin auth.
    """
    acl_path = Path(ACL_FILE_PATH)
    if not acl_path.exists():
        return None

    try:
        content = acl_path.read_text()
    except OSError:
        return None

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"user {username} "):
            # Extract password from "user {name} on >{password} ..."
            import re
            match = re.search(rf"^user {re.escape(username)}\s+on\s+>([^ ]+)", stripped)
            if match:
                return match.group(1)
    return None
