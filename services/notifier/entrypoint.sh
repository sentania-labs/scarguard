#!/bin/sh
set -e
# Fix volume ownership — setup.sh creates volumes as root; the scarguard
# user needs write access to /var/lib/scarguard (notifier state).
# Only run the recursive chown once (sentinel file prevents slow restarts).
if [ ! -f /var/lib/scarguard/.ownership-fixed ]; then
    chown -R scarguard:scarguard /var/lib/scarguard 2>/dev/null || echo "WARNING: chown failed on /var/lib/scarguard — check volume mounts" >&2
    chown -R scarguard:scarguard /data /config 2>/dev/null || echo "WARNING: chown failed on /data or /config — check volume mounts" >&2
    touch /var/lib/scarguard/.ownership-fixed 2>/dev/null || true
else
    # On subsequent starts, just fix top-level dirs (fast)
    chown scarguard:scarguard /data /config /var/lib/scarguard 2>/dev/null || true
fi
exec gosu scarguard "$@"
