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

# Common snippet: security headers + probe-path deny + reverse proxy.
#
# NOTE: this is the *active* Caddy config — config/Caddyfile.template is
# a reference document only and is NOT read at runtime.  Keep any config
# changes here, not there (an earlier attempt in v0.12.8 edited the
# template and had no effect in production).
#
# NOTE: Caddy's `caddy fmt` linter expects tab indentation. Edit with care —
# replacing tabs with spaces will resurrect the "Caddyfile input is not
# formatted" warning v0.12.10 cleared (the v0.12.8 fmt fix was applied to
# Caddyfile.template by mistake and never reached the active config).
tls_active = mode in ("auto", "manual")
hsts_header = (
    '\t\tStrict-Transport-Security "max-age=31536000; includeSubDomains"\n'
    if tls_active else ""
)

# v1.15: optional config-api service split.  When system.config_api.enabled
# is true in scarguard.yml, Caddy routes the 8 config-write POST paths to
# the config-api container instead of web.  When disabled (default), the
# snippet contains only the standard reverse_proxy to web:8080.
sys_cfg = cfg.get("system", {})
if not isinstance(sys_cfg, dict):
    sys_cfg = {}
config_api_cfg = sys_cfg.get("config_api", {})
if not isinstance(config_api_cfg, dict):
    config_api_cfg = {}
config_api_enabled = config_api_cfg.get("enabled", False) is True

if config_api_enabled:
    config_api_block = """
\t# Config-API split: route config-write POSTs to the dedicated service.
\t@config_writes {
\t\tmethod POST
\t\tpath /config/structured /config /config/tls/upload-cert /config/test-notification /admin/deterrent /admin/backups/create /admin/db-backups/trigger
\t}
\t@config_restore {
\t\tmethod POST
\t\tpath_regexp restore ^/admin/backups/[^/]+/restore$
\t}
\treverse_proxy @config_writes config-api:8081
\treverse_proxy @config_restore config-api:8081
"""
    print("[caddy-entrypoint] Config-API split enabled — routing write POSTs to config-api:8081", file=sys.stderr)
else:
    config_api_block = ""

snippet = """(scarguard) {
\theader {
\t\tX-Frame-Options DENY
\t\tX-Content-Type-Options nosniff
\t\tReferrer-Policy strict-origin-when-cross-origin
\t\tPermissions-Policy "geolocation=(), camera=(), microphone=(), payment=()"
""" + hsts_header + """\t\tContent-Security-Policy "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'"
\t\tCross-Origin-Opener-Policy same-origin
\t\tCross-Origin-Resource-Policy same-origin
\t\t-Server
\t}
\t# Drop common bot-probe paths at the edge so they never reach FastAPI.
\t# 404 (not 403) is intentional — quieter, looks like the path doesn't
\t# exist so scanners are less likely to follow up with more probes.
\t@probes {
\t\tpath /.git/* /_ignition/* /aws*config.js /config.js
\t}
\trespond @probes 404
""" + config_api_block + """\treverse_proxy web:8080
}
"""

if mode == "auto" and domain:
    body = f"""{snippet}
{domain} {{
\timport scarguard
}}
"""
elif mode == "manual":
    # Verify cert/key exist; fall back to HTTP if missing.
    cert_ok = pathlib.Path(cert_path).exists()
    key_ok = pathlib.Path(key_path).exists()
    if cert_ok and key_ok:
        body = f"""{snippet}
:443 {{
\ttls {cert_path} {key_path}
\timport scarguard
}}

:80 {{
\tredir https://{{host}}{f":{https_port}" if https_port != "443" else ""}{{uri}} permanent
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
\timport scarguard
}}
"""
else:
    if mode not in ("off", ""):
        print(f"[caddy-entrypoint] Unknown tls.mode '{mode}' — defaulting to HTTP", file=sys.stderr)
    body = f"""{snippet}
:80 {{
\timport scarguard
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
