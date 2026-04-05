#!/bin/sh
set -e
# Fix volume ownership — setup.sh creates volumes as root; the scarguard
# user needs write access to /data (SQLite, snapshots, auth.db) and /config.
# Only run the recursive chown once (sentinel file prevents slow restarts
# when /data/snapshots contains thousands of images).
# When running as non-root already (e.g. CI --user root override removed),
# skip chown/gosu and just exec the command directly.
if [ "$(id -u)" = "0" ]; then
    if [ ! -f /data/.ownership-fixed ]; then
        chown -R scarguard:scarguard /data /models 2>/dev/null || echo "WARNING: chown failed on /data or /models — check volume mounts" >&2
        chown -R scarguard:scarguard /config 2>/dev/null || echo "WARNING: chown failed on /config — check volume mounts" >&2
        touch /data/.ownership-fixed 2>/dev/null || true
    else
        # On subsequent starts, just fix top-level dirs (fast)
        chown scarguard:scarguard /data /config /models 2>/dev/null || true
    fi
    exec gosu scarguard "$@"
fi
exec "$@"
