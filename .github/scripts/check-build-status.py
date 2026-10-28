#!/usr/bin/env python3
"""Check that every build.yml image job has passed on this commit."""
import json
import os
import re
import sys
import urllib.request

build_pattern = re.compile(os.environ.get("BUILD_NAME_FILTER", r"^Build .+ image"))
base_url = f"https://api.github.com/repos/{os.environ.get('GITHUB_REPOSITORY', '')}/commits/{os.environ.get('GITHUB_SHA', '')}/check-runs"
headers = {"Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}"}

check_runs = []
page = 1
try:
    while True:
        req = urllib.request.Request(f"{base_url}?per_page=100&page={page}", headers=headers)
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
        runs = data.get("check_runs", [])
        check_runs.extend(runs)
        if len(check_runs) >= data.get("total_count", 0) or not runs:
            break
        page += 1
except Exception as e:
    print(f"FAIL: Could not fetch check-runs: {e}")
    sys.exit(1)

matched = [run for run in check_runs if build_pattern.search(run.get("name", ""))]
if not matched:
    print(f"FAIL: No check found matching {build_pattern.pattern}")
    sys.exit(1)

failed = False
for run in matched:
    name = run.get("name", "")
    status = run.get("status", "")
    conclusion = run.get("conclusion", "")
    if status == "completed" and conclusion == "success":
        print(f"OK: {name} succeeded")
    else:
        print(f"FAIL: {name} status={status} conclusion={conclusion}")
        failed = True
sys.exit(1 if failed else 0)
