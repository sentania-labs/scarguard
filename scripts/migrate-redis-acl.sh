#!/usr/bin/env bash
# scripts/migrate-redis-acl.sh - staged migration to per-service Redis ACL users.
#
# Stage 1 (always): backfill one REDIS_PASSWORD_<SERVICE> credential per
#   Redis-connected service into the env file. Existing values are kept, the
#   admin REDIS_PASSWORD is untouched, and no credential is ever printed.
# Stage 2 (--apply): recreate the redis container so it loads the ACL file,
#   then recreate every other service so each one receives only its own
#   credential. Until a service is recreated it keeps authenticating with the
#   old shared password as the "default" user, so the order is safe.
#
# Usage:
#   scripts/migrate-redis-acl.sh [ENV_FILE] [--apply]
#
# setup.sh runs stage 1 on every install and upgrade. Without stage 2 the
# next `docker compose up -d` applies the new credentials; a service whose
# credential is missing is created DISABLED in Redis (fail closed) and named
# in the redis container log.
set -euo pipefail

ENV_FILE=".env"
APPLY=false
for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=true ;;
        *) ENV_FILE="$arg" ;;
    esac
done

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Environment file not found: $ENV_FILE" >&2
    exit 1
fi

# Must match the user names in config/redis-acl.conf ('-' becomes '_').
SERVICES=(detector web notifier deterrent off-watchdog backup log-streamer training-controller trainer)

created=()
for svc in "${SERVICES[@]}"; do
    var="REDIS_PASSWORD_$(printf '%s' "$svc" | tr 'a-z-' 'A-Z_')"
    if grep -q "^${var}=.\{32\}" "$ENV_FILE"; then
        continue
    fi
    value=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
    if grep -q "^${var}=" "$ENV_FILE"; then
        sed -i "s|^${var}=.*|${var}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$var" "$value" >> "$ENV_FILE"
    fi
    unset value
    created+=("$var")
done

if [[ ${#created[@]} -gt 0 ]]; then
    echo "redis-acl: generated ${#created[@]} per-service credential(s) in $ENV_FILE: ${created[*]}"
else
    echo "redis-acl: all per-service credentials already present in $ENV_FILE"
fi

if [[ "$APPLY" == "true" ]]; then
    echo "redis-acl: recreating redis with the ACL file"
    docker compose up -d --force-recreate --no-deps redis
    echo "redis-acl: recreating services with their own credentials"
    docker compose up -d --force-recreate
    echo "redis-acl: done - check 'docker compose logs redis' for disabled users"
else
    echo "redis-acl: next step: docker compose up -d   (or re-run with --apply)"
fi
