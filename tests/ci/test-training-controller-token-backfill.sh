#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
TEST_DIR=$(mktemp -d)
trap 'rm -rf -- "$TEST_DIR"' EXIT

for CASE_NAME in missing empty; do
    ENV_FILE="$TEST_DIR/$CASE_NAME.env"
    printf 'REDIS_PASSWORD=test-redis-password\n' > "$ENV_FILE"
    if [[ "$CASE_NAME" == "empty" ]]; then
        printf 'TRAINING_CONTROLLER_TOKEN=\n' >> "$ENV_FILE"
    fi

    bash "$REPO_ROOT/infra/backfill-training-controller-token.sh" "$ENV_FILE"
    CONTROLLER_TOKEN=$(sed -n 's/^TRAINING_CONTROLLER_TOKEN=//p' "$ENV_FILE")
    if (( ${#CONTROLLER_TOKEN} < 32 )); then
        echo "Backfill produced an invalid token for $CASE_NAME input" >&2
        exit 1
    fi

    RENDERED=$(docker compose \
        --project-directory "$REPO_ROOT" \
        --profile training \
        --env-file "$ENV_FILE" \
        config --format json)
    EXPECTED="\"TRAINING_CONTROLLER_TOKEN\": \"$CONTROLLER_TOKEN\""
    if [[ $(grep -oF "$EXPECTED" <<< "$RENDERED" | wc -l) -ne 2 ]]; then
        echo "Backfilled token was not rendered into controller and trainer for $CASE_NAME input" >&2
        exit 1
    fi
done
