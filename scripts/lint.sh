#!/usr/bin/env bash
# Shared local/CI scope. Include every service's source AND tests.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m ruff check services/*/src services/*/tests shared training "$@"
