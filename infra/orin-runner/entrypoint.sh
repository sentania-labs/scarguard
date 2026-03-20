#!/usr/bin/env bash
set -euo pipefail

# --- Configuration via environment variables ---
# RUNNER_TOKEN:   One-time runner registration token from GitHub
# RUNNER_NAME:    Display name for this runner (default: hostname)
# RUNNER_LABELS:  Comma-separated labels (default: "self-hosted,linux,arm64,jetson")
# REPO_URL:       Full GitHub URL (org or repo level)
#                 Org:  https://github.com/your-org
#                 Repo: https://github.com/your-org/scarguard

RUNNER_NAME="${RUNNER_NAME:-$(hostname)}"
RUNNER_LABELS="${RUNNER_LABELS:-self-hosted,linux,arm64,jetson}"

echo "=== ScarGuard Orin Runner ==="
echo "  URL:    ${REPO_URL}"
echo "  Name:   ${RUNNER_NAME}"
echo "  Labels: ${RUNNER_LABELS}"
echo ""

# --- Configure the runner (only if not already configured) ---
if [ ! -f ".runner" ]; then
    echo "First run — configuring runner..."
    ./config.sh \
        --url "${REPO_URL}" \
        --token "${RUNNER_TOKEN}" \
        --name "${RUNNER_NAME}" \
        --labels "${RUNNER_LABELS}" \
        --unattended \
        --replace
else
    echo "Runner already configured, skipping registration."
fi

# --- Cleanup on exit ---
cleanup() {
    echo ""
    echo "Caught signal, shutting down runner..."
}
trap cleanup SIGTERM SIGINT

# --- Start the runner ---
./run.sh &
wait $!