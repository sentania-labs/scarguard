#!/usr/bin/env bash
# scripts/ci/check_typecheck_isolation.sh
# Verify that the typecheck job in .github/workflows/ci.yml never installs
# more than one service's requirements into a single environment.
#
# The bad pattern is a single run: block that lists two or more
# -r services/<name>/requirements.txt entries in one pip install command.
set -euo pipefail
cd "$(dirname "$0")/../.."

WORKFLOW=".github/workflows/ci.yml"

if [ ! -f "$WORKFLOW" ]; then
  echo "FAIL: $WORKFLOW not found" >&2
  exit 1
fi

# Parse the workflow with PyYAML (loaded via uv run).
uv run --no-project --with pyyaml -- python3 -c "
import yaml, re, sys

with open('$WORKFLOW') as f:
    wf = yaml.safe_load(f)

jobs = wf.get('jobs', {})
tc = jobs.get('typecheck', {})
steps = tc.get('steps', [])

for step in steps:
    name = step.get('name', '')
    run_block = step.get('run', '')

    # Inspect every run block for requirement-file installs.
    # The isolation check must not depend on the step name being
    # 'Install deps' -- any name could carry multiple -r flags.
    if '-r services/' not in run_block:
        continue

    matches = re.findall(r'-r\s+services/([^/]+)/requirements\.txt', run_block)
    if len(matches) > 1:
        print(
            f'FAIL: Step \"' + name + '\" installs requirements for '
            f'{len(matches)} services (' + ', '.join(matches) + ') '
            f'into one environment. Each service must have its own '
            f'virtual environment.'
        )
        sys.exit(1)

print('OK: typecheck job installs each service into its own environment')
"
