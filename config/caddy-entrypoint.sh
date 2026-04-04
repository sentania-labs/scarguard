#!/bin/sh
# ScarGuard — Caddy entrypoint
#
# Reads the tls section from scarguard.yml, generates a Caddyfile, starts
# Caddy, and watches for config changes (same mtime-polling pattern used by
# the detector and notifier services).
#
# TLS modes:
#   off     — HTTP only on :80 (default)
#   auto    — Let's Encrypt via domain name
#   manual  — User-provided cert/key files
#
# Requires: caddy, python3 (Alpine base includes both in the Caddy image
# when python3 is added; see docker-compose.yml).

set -e

CONFIG_PATH="${CONFIG_PATH:-/config/scarguard.yml}"
CADDYFILE="/etc/caddy/Caddyfile"

# ── Generate Caddyfile from scarguard.yml ──────────────────────────────────

generate_caddyfile() {
    python3 - "$CONFIG_PATH" "$CADDYFILE" <<'PYEOF'
import os, sys, yaml, pathlib

config_path = sys.argv[1]
caddyfile_path = sys.argv[2]

tls_cfg = {}
try:
    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    tls_cfg = cfg.get("tls", {})

except Exception as e:
    print(f"[caddy-entrypoint] Warning: cannot read {config_path}: {e}", file=sys.stderr)

mode = str(tls_cfg.get("mode", "off")).lower()
domain = str(tls_cfg.get("domain", "")).strip()
cert_path = str(tls_cfg.get("cert_path", "/config/certs/cert.pem"))
key_path = str(tls_cfg.get("key_path", "/config/certs/key.pem"))
https_port = os.environ.get("HTTPS_PORT", "443").strip()

# Common snippet: security headers + reverse proxy
snippet = """(scarguard) {
    header {
        X-Frame-Options DENY
        X-Content-Type-Options nosniff
        Referrer-Policy strict-origin-when-cross-origin
        Content-Security-Policy "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' https://unpkg.com"
        -Server
    }
    reverse_proxy web:8080
}
"""

if mode == "auto" and domain:
    body = f"""{snippet}
{domain} {{
    import scarguard
}}
"""
elif mode == "manual":
    # Verify cert/key exist; fall back to HTTP if missing.
    cert_ok = pathlib.Path(cert_path).exists()
    key_ok = pathlib.Path(key_path).exists()
    if cert_ok and key_ok:
        body = f"""{snippet}
:443 {{
    tls {cert_path} {key_path}
    import scarguard
}}

:80 {{
    redir https://{{host}}{f":{https_port}" if https_port != "443" else ""}{{uri}} permanent
}}
"""
    else:
        missing = []
        if not cert_ok:
            missing.append(f"cert ({cert_path})")
        if not key_ok:
            missing.append(f"key ({key_path})")
        print(f"[caddy-entrypoint] TLS mode=manual but missing: {', '.join(missing)} — falling back to HTTP", file=sys.stderr)
        body = f"""{snippet}
:80 {{
    import scarguard
}}
"""
else:
    if mode not in ("off", ""):
        print(f"[caddy-entrypoint] Unknown tls.mode '{mode}' — defaulting to HTTP", file=sys.stderr)
    body = f"""{snippet}
:80 {{
    import scarguard
}}
"""

pathlib.Path(caddyfile_path).write_text(body)
print(f"[caddy-entrypoint] Generated Caddyfile (mode={mode})", file=sys.stderr)
PYEOF
}

# ── Config watcher (background) ───────────────────────────────────────────
# Polls scarguard.yml mtime every 5 seconds. On change, regenerates the
# Caddyfile and sends SIGHUP to Caddy for a graceful reload.

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
            echo "[caddy-entrypoint] Config changed — regenerating Caddyfile and reloading Caddy" >&2
            generate_caddyfile
            # Caddy reloads config from the Caddyfile via its admin API.
            caddy reload --config "$CADDYFILE" --adapter caddyfile 2>&1 || \
                echo "[caddy-entrypoint] WARNING: Caddy reload failed — check Caddyfile syntax" >&2
            LAST_MTIME="$CURRENT_MTIME"
        fi
    done
}

# ── Main ───────────────────────────────────────────────────────────────────

generate_caddyfile
watch_config &
exec caddy run --config "$CADDYFILE" --adapter caddyfile
