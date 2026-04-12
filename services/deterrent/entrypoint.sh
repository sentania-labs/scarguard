#!/bin/sh
set -e
# Fix volume ownership — the scarguard user needs write access to /data
# for SQLite (battery status).  Only run the recursive chown once (sentinel
# file prevents slow restarts).
if [ "$(id -u)" = "0" ]; then
    if [ ! -f /data/.ownership-fixed-deterrent ]; then
        chown -R scarguard:scarguard /data 2>/dev/null || echo "WARNING: chown failed on /data — check volume mounts" >&2
        chown -R scarguard:scarguard /config 2>/dev/null || echo "WARNING: chown failed on /config — check volume mounts" >&2
        touch /data/.ownership-fixed-deterrent 2>/dev/null || true
    else
        # On subsequent starts, just fix top-level dirs (fast)
        chown scarguard:scarguard /data /config 2>/dev/null || true
    fi
    exec gosu scarguard "$@"
fi
exec "$@"
