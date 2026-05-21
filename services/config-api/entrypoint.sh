#!/bin/sh
set -e
# Fix volume ownership — same pattern as web service.
# Needs write access to /config (scarguard.yml, certs, backups) and /data.
if [ "$(id -u)" = "0" ]; then
    if [ ! -f /data/.ownership-fixed-config-api ]; then
        chown -R scarguard:scarguard /data 2>/dev/null || echo "WARNING: chown failed on /data — check volume mounts" >&2
        chown -R scarguard:scarguard /config 2>/dev/null || echo "WARNING: chown failed on /config — check volume mounts" >&2
        touch /data/.ownership-fixed-config-api 2>/dev/null || true
    else
        chown scarguard:scarguard /data /config 2>/dev/null || true
    fi
    exec gosu scarguard "$@"
fi
exec "$@"
