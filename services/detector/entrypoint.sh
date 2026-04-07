#!/bin/sh
set -e
# Fix volume ownership — setup.sh creates volumes as root; the scarguard
# user needs write access to /data (SQLite + snapshots).
# Only run the recursive chown once (sentinel file prevents slow restarts
# when /data/snapshots contains thousands of images).
# When running as non-root already (e.g. CI --user root override removed),
# skip chown/gosu and just exec the command directly.
if [ "$(id -u)" = "0" ]; then
    if [ ! -f /data/.ownership-fixed-detector ]; then
        chown -R scarguard:scarguard /data /models 2>/dev/null || echo "WARNING: chown failed on /data or /models — check volume mounts" >&2
        chown -R scarguard:scarguard /config 2>/dev/null || echo "WARNING: chown failed on /config — check volume mounts" >&2
        touch /data/.ownership-fixed-detector 2>/dev/null || true
    else
        # On subsequent starts, just fix top-level dirs (fast)
        chown scarguard:scarguard /data /config /models 2>/dev/null || true
    fi

    # Clean up any stale predict{N} dirs that may have accumulated from a
    # pre-v0.12.7 detector sharing the same overlay layer.  Without this, an
    # in-place container upgrade would inherit the old dirs, and even though
    # the v0.12.7 code uses exist_ok=True, ultralytics still stats predict,
    # predict2, ..., predict{N} on the first call before finding its free
    # slot.  See INFERENCE_INVESTIGATION.md.
    #
    # The glob 'predict[0-9]*' intentionally matches predict2..predict9998
    # but NOT the bare 'predict' directory we create immediately above —
    # that's the single directory all pinned save_dirs now resolve to.
    mkdir -p /tmp/runs/predict
    stale_count=$(find /tmp/runs -mindepth 1 -maxdepth 1 -type d -name 'predict[0-9]*' 2>/dev/null | wc -l)
    if [ "$stale_count" -gt 0 ]; then
        echo "detector-entrypoint: cleaning $stale_count stale predict[N] dirs in /tmp/runs"
        find /tmp/runs -mindepth 1 -maxdepth 1 -type d -name 'predict[0-9]*' -exec rm -rf {} + 2>/dev/null || true
    fi
    chown -R scarguard:scarguard /tmp/runs 2>/dev/null || true

    exec gosu scarguard "$@"
fi
exec "$@"
