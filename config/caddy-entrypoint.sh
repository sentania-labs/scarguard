#!/bin/sh
# ScarGuard - Caddy entrypoint
#
# Reads the tls section from scarguard.yml, generates a Caddyfile, starts
# Caddy, and watches for config changes (same mtime-polling pattern used by
# the detector and notifier services).
#
# TLS modes:
#   off     - HTTP only on :80 (default)
#   auto    - Let's Encrypt via domain name
#   manual  - User-provided cert/key files
#
# Requires: caddy, python3 + py3-yaml (added in services/caddy/Dockerfile).

set -e

CONFIG_PATH="${CONFIG_PATH:-/config/scarguard.yml}"
CADDYFILE="/etc/caddy/Caddyfile"

# ── Generate Caddyfile from scarguard.yml ──────────────────────────────────
#
# NOTE: the Caddyfile is rendered by config/caddy_config.py (copied into the
# image next to shared/tls_safety.py) - config/Caddyfile.template is a
# reference document only and is NOT read at runtime.  caddy_config.py
# validates every tls value before it reaches the Caddyfile, runs
# `caddy validate` on the result, and on reload keeps the current config
# (and a CADDYFILE.last-good copy) whenever the new one is refused.

CADDY_CONFIG_PY="${CADDY_CONFIG_PY:-/usr/local/lib/scarguard/caddy_config.py}"

generate_caddyfile() {
    python3 "$CADDY_CONFIG_PY" generate "$CONFIG_PATH" "$CADDYFILE"
}

# ── Config watcher (background) ───────────────────────────────────────────
# Polls scarguard.yml mtime every 5 seconds. On change, validates and
# applies the new Caddyfile through Caddy's admin API (graceful reload).

watch_config() {
    LAST_MTIME=""
    if [ -f "$CONFIG_PATH" ]; then
        LAST_MTIME=$(stat -c %Y "$CONFIG_PATH" 2>/dev/null || echo "")
    fi

    while true; do
        sleep 5
        CURRENT_MTIME=""
        if [ -f "$CONFIG_PATH" ]; then
            CURRENT_MTIME=$(stat -c %Y "$CONFIG_PATH" 2>/dev/null || echo "")
        fi

        if [ "$CURRENT_MTIME" != "$LAST_MTIME" ] && [ -n "$CURRENT_MTIME" ]; then
            echo "[caddy-entrypoint] Config changed - validating new Caddyfile before reload" >&2
            # Renders, runs `caddy validate`, swaps the file atomically and
            # reloads via the admin API; non-zero means the current config
            # was kept.
            python3 "$CADDY_CONFIG_PY" reload "$CONFIG_PATH" "$CADDYFILE" || \
                echo "[caddy-entrypoint] WARNING: new config refused - Caddy keeps its current config" >&2
            LAST_MTIME="$CURRENT_MTIME"
        fi
    done
}

# ── Main ───────────────────────────────────────────────────────────────────

generate_caddyfile
watch_config &
exec caddy run --config "$CADDYFILE" --adapter caddyfile
