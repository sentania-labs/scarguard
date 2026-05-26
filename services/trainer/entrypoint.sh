#!/bin/sh
set -e
if [ "$(id -u)" = "0" ]; then
    if [ ! -f /data/.ownership-fixed-trainer ]; then
        chown -R scarguard:scarguard /data /models 2>/dev/null || echo "WARNING: chown failed on /data or /models" >&2
        touch /data/.ownership-fixed-trainer 2>/dev/null || true
    else
        chown scarguard:scarguard /data /config /models 2>/dev/null || true
    fi
    mkdir -p /tmp/runs/predict /data/training_workspace /data/training_uploads
    chown -R scarguard:scarguard /tmp/runs /data/training_workspace /data/training_uploads 2>/dev/null || true
    exec gosu scarguard "$@"
fi
exec "$@"
