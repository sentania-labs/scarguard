#!/bin/sh
# ScarGuard Redis entrypoint (runs inside the stock redis:7-alpine image).
#
# Builds the ACL file from config/redis-acl.conf plus per-service credentials
# delivered as REDIS_PASSWORD_<SERVICE> environment variables, then starts
# redis-server with that aclfile and a non-evicting memory policy.
#
# - Only SHA-256 digests of credentials are written to the ACL file; no
#   credential is ever printed.
# - REDIS_PASSWORD (admin, "default" user) stays inside this container; it is
#   used only by the healthcheck and operator `docker exec` sessions.
# - Fail closed: when REDIS_PASSWORD is set but a service credential is
#   missing, that service user is created disabled ("off") and named in the
#   log so the operator can run scripts/migrate-redis-acl.sh.
# - When REDIS_PASSWORD is empty (CI smoke test / local dev), users are
#   created with "nopass" so the stack still starts, but command, key and
#   channel limits are still enforced.
#
# Extra arguments are appended to redis-server (tests use --port/--dir).
set -eu

POLICY="${SCARGUARD_REDIS_ACL_POLICY:-/scarguard/redis-acl.conf}"
ACL_FILE="${SCARGUARD_REDIS_ACL_FILE:-/data/scarguard.acl}"
MAXMEMORY="${SCARGUARD_REDIS_MAXMEMORY:-200mb}"

digest() {
    h=$(printf '%s' "$1" | sha256sum | cut -d' ' -f1)
    if [ "${#h}" -ne 64 ]; then
        echo "scarguard-redis: sha256sum unavailable - refusing to write ACL file" >&2
        exit 1
    fi
    printf '%s' "$h"
}

if [ ! -r "$POLICY" ]; then
    echo "scarguard-redis: ACL policy $POLICY is missing or unreadable - refusing to start" >&2
    exit 1
fi

umask 077
tmp="$ACL_FILE.tmp"
admin="${REDIS_PASSWORD:-}"
if [ -n "$admin" ]; then
    mode=enforced
    printf 'user default on #%s ~* &* +@all\n' "$(digest "$admin")" > "$tmp"
else
    mode=open
    printf 'user default on nopass ~* &* +@all\n' > "$tmp"
    echo "scarguard-redis: REDIS_PASSWORD is empty - running WITHOUT authentication (CI/dev only); ACL command, key and channel limits still apply" >&2
fi
unset admin

disabled=""
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        ''|'#'*) continue ;;
    esac
    name="${line%% *}"
    rules="${line#* }"
    case "$name" in
        *[!a-z0-9-]*|'')
            echo "scarguard-redis: invalid user name in $POLICY: '$name'" >&2
            exit 1
            ;;
    esac
    var="REDIS_PASSWORD_$(printf '%s' "$name" | tr 'a-z-' 'A-Z_')"
    eval "secret=\${$var:-}"
    if [ -n "$secret" ]; then
        printf 'user %s on #%s %s\n' "$name" "$(digest "$secret")" "$rules" >> "$tmp"
    elif [ "$mode" = open ]; then
        printf 'user %s on nopass %s\n' "$name" "$rules" >> "$tmp"
    else
        printf 'user %s off %s\n' "$name" "$rules" >> "$tmp"
        disabled="$disabled $name($var)"
    fi
    unset secret
done < "$POLICY"

if [ -n "$disabled" ]; then
    echo "scarguard-redis: credentials missing, these users are DISABLED until scripts/migrate-redis-acl.sh runs and the stack is recreated:$disabled" >&2
fi

mv -f "$tmp" "$ACL_FILE"
echo "scarguard-redis: ACL file written ($mode mode), maxmemory=$MAXMEMORY policy=noeviction" >&2
exec redis-server --aclfile "$ACL_FILE" --maxmemory "$MAXMEMORY" --maxmemory-policy noeviction "$@"
