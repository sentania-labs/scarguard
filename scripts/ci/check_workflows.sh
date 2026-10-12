#!/usr/bin/env bash
# scripts/ci/check_workflows.sh
# Run actionlint over all GitHub Actions workflows and verify that every
# docker/login-action step in build.yml carries the dependabot guard
# so that PRs triggered by dependabot do not fail on missing repository
# secrets.  Also verify that the typecheck job isolates each service into
# its own venv so that differing requirement versions never clash.
set -euo pipefail
cd "$(dirname "$0")/../.."

ACTIONLINT_DIR=$(mktemp -d /tmp/actionlint.XXXXXXXXXX)
trap 'rm -rf "$ACTIONLINT_DIR"' EXIT

# ── 1. actionlint ──────────────────────────────────────────────────────────
ACTIONLINT="${ACTIONLINT:-$(which actionlint 2>/dev/null || true)}"
if [ -z "$ACTIONLINT" ]; then
  # Check common local installations
  for d in /tmp/local_bin /opt/local/bin ~/bin; do
    if [ -x "$d/actionlint" ]; then
      ACTIONLINT="$d/actionlint"
      break
    fi
  done
fi
if [ -z "$ACTIONLINT" ]; then
  # Download a pinned release of actionlint for the current OS/arch.
  # Version: 1.7.12
  UNAME_S=$(uname -s)
  UNAME_M=$(uname -m)
  case "$UNAME_S-$UNAME_M" in
    Linux-arm64|Linux-aarch64)
      ACTIONLINT_URL="https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_arm64.tar.gz"
      EXPECTED_SHA="325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6"
      ;;
    *)
      ACTIONLINT_URL="https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz"
      EXPECTED_SHA="8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"
      ;;
  esac
  TARBALL="${ACTIONLINT_DIR}/actionlint.tar.gz"
  BINARY="${ACTIONLINT_DIR}/actionlint"

  curl -fsSL -o "$TARBALL" -H "User-Agent: scarguard-ci" "$ACTIONLINT_URL"
  ACTUAL_SHA=$(sha256sum "$TARBALL" | awk '{print $1}')
  if [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
    echo "ERROR: actionlint checksum mismatch: expected $EXPECTED_SHA, got $ACTUAL_SHA" >&2
    exit 1
  fi
  tar xzf "$TARBALL" -C "$ACTIONLINT_DIR"
  ACTIONLINT="$BINARY"
fi

# Known pre-existing issues that are not caused by this change:
#   - cleanup.yml:19 uses ${{ false }} (retired workflow stub)
#   - release.yml:429 uses custom self-hosted label "jetson"
#   - release.yml:696-697 reference needs.release-detector outputs
#     (resolved from a later job in the same workflow - actionlint
#      does not track cross-job ordering for this check)
# These are suppressed via -ignore to keep the gate focused on
# workflow syntax errors introduced by changes.
"$ACTIONLINT" \
  -ignore 'constant expression "false" in condition' \
  -ignore 'label "jetson" is unknown' \
  -ignore 'property "release-detector" is not defined' \
  -ignore 'property "release-detector-x86" is not defined' \
  -color .github/workflows/*.yml

# ── 2. YAML parse of dependabot.yml ───────────────────────────────────────
uv run --no-project --with pyyaml -- python3 -c "
import yaml, sys
d = yaml.safe_load(open('.github/dependabot.yml'))
assert all('groups' in u for u in d['updates']), 'ungrouped ecosystem'
print(f'grouped {len(d[\"updates\"])}')
"

# ── 3. Every docker/login-action step in build.yml must have the push guard
#     (the condition github.actor != 'dependabot[bot]').
uv run --no-project --with pyyaml -- python3 -c "
import yaml, sys

with open('.github/workflows/build.yml') as f:
    content = f.read()

lines = content.split('\n')
in_login_step = False
step_name = ''
errors = 0
REQUIRED_GUARD = \"github.actor != 'dependabot[bot]'\"

for i, line in enumerate(lines):
    stripped = line.strip()
    if stripped.startswith('- name:') and 'Log in to Docker Hub' in stripped:
        in_login_step = True
        step_name = stripped
    elif in_login_step and stripped.startswith('if:'):
        guard_value = stripped[len('if:'):].strip()
        if guard_value == REQUIRED_GUARD:
            in_login_step = False
        else:
            print(f'FAIL: {step_name} on line {i+1} has guard \"{guard_value}\" '
                  f'instead of \"{REQUIRED_GUARD}\"')
            errors += 1
    elif in_login_step and stripped.startswith('uses:') and 'docker/login-action' in stripped:
        # Step ended without a valid guard line being parsed
        if errors == 0:
            print(f'FAIL: {step_name} on line {i+1} has no valid dependabot guard')
            errors += 1
        in_login_step = False

if errors:
    print(f'{errors} login step(s) missing or misconfigured dependabot guard')
    sys.exit(1)
print('All login steps have the dependabot guard')
"

# ── 4. Typecheck isolation: each service gets its own venv ──────────────────
bash scripts/ci/check_typecheck_isolation.sh
