#!/usr/bin/env bash
set -euo pipefail

# --- Configuration via environment variables ---
# GITHUB_OWNER:   GitHub org or user (e.g., "sentania")
# GITHUB_REPO:    Repository name (e.g., "scarguard") — omit for org-level runner
# GITHUB_TOKEN:   Personal access token or registration token
# RUNNER_NAME:    Display name for this runner (default: hostname)
# RUNNER_LABELS:  Comma-separated labels (default: "self-hosted,linux,arm64,jetson")
# RUNNER_GROUP:   Runner group (default: "Default")

RUNNER_NAME="${RUNNER_NAME:-$(hostname)}"
RUNNER_LABELS="${RUNNER_LABELS:-self-hosted,linux,arm64,jetson}"
RUNNER_GROUP="${RUNNER_GROUP:-Default}"

# Determine registration URL and API endpoint
if [ -n "${GITHUB_REPO:-}" ]; then
    RUNNER_URL="https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}"
    API_URL="https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/runners/registration-token"
else
    RUNNER_URL="https://github.com/${GITHUB_OWNER}"
    API_URL="https://api.github.com/orgs/${GITHUB_OWNER}/actions/runners/registration-token"
fi

echo "=== ScarGuard Orin Runner ==="
echo "  URL:    ${RUNNER_URL}"
echo "  Name:   ${RUNNER_NAME}"
echo "  Labels: ${RUNNER_LABELS}"
echo ""

# --- Get registration token ---
echo "Requesting registration token..."
REG_TOKEN=$(curl -s -X POST \
    -H "Authorization: token ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    "${API_URL}" | jq -r .token)

if [ "${REG_TOKEN}" = "null" ] || [ -z "${REG_TOKEN}" ]; then
    echo "ERROR: Failed to get registration token. Check GITHUB_TOKEN permissions."
    echo "  Token needs: admin:org scope (org runner) or repo scope (repo runner)"
    exit 1
fi

# --- Configure the runner ---
./config.sh \
    --url "${RUNNER_URL}" \
    --token "${REG_TOKEN}" \
    --name "${RUNNER_NAME}" \
    --labels "${RUNNER_LABELS}" \
    --runnergroup "${RUNNER_GROUP}" \
    --unattended \
    --replace

# --- Cleanup on exit ---
cleanup() {
    echo ""
    echo "Caught signal, removing runner..."
    ./config.sh remove --token "${REG_TOKEN}" || true
}
trap cleanup SIGTERM SIGINT

# --- Start the runner ---
./run.sh &
wait $!
