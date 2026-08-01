#!/usr/bin/env bash
# Backfill the narrowly scoped training-controller credential for upgrades.
set -euo pipefail

ENV_FILE="${1:-.env}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Environment file not found: $ENV_FILE" >&2
    exit 1
fi

if grep -q '^TRAINING_CONTROLLER_TOKEN=.\{32\}' "$ENV_FILE"; then
    exit 0
fi

CONTROLLER_TOKEN=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
if grep -q '^TRAINING_CONTROLLER_TOKEN=' "$ENV_FILE"; then
    sed -i "s|^TRAINING_CONTROLLER_TOKEN=.*|TRAINING_CONTROLLER_TOKEN=${CONTROLLER_TOKEN}|" "$ENV_FILE"
else
    printf 'TRAINING_CONTROLLER_TOKEN=%s\n' "$CONTROLLER_TOKEN" >> "$ENV_FILE"
fi
