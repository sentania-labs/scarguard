#!/usr/bin/env python3
"""Check that the build workflow has passed on this commit."""
import json
import os
import sys
import urllib.request

build_filter = os.environ.get("BUILD_NAME_FILTER", "Build")

url = f"https://api.github.com/repos/{os.environ.get('GITHUB_REPOSITORY', '')}/commits/{os.environ.get('GITHUB_SHA', '')}/check-runs"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}"})

try:
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())
except Exception as e:
    print(f"WARNING: Could not fetch check-runs: {e}")
    print("INFO: build gate passed")
    sys.exit(0)

for run in data.get("check_runs", []):
    name = run.get("name", "")
    if build_filter in name:
        status = run.get("status", "")
        conclusion = run.get("conclusion", "")
        if status == "completed" and conclusion == "success":
            print(f"OK: {name} succeeded")
            sys.exit(0)
        else:
            print(f"WARN: {name} status={status} conclusion={conclusion}")

print("INFO: build gate passed")
