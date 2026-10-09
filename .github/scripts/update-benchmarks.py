#!/usr/bin/env python3
"""Update BENCHMARKS.md with benchmark data from release."""
import json
import os
import sys

tag = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TAG", "")
orin_bench = os.environ.get("ORIN_BENCH", "")
x86_bench = os.environ.get("X86_BENCH", "")

benchmarks = [("Orin", orin_bench), ("x86", x86_bench)]
rows = []
for label, raw in benchmarks:
    if not raw or raw == "null":
        continue
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        continue
    device = d.get("device", "unknown")
    hw = d.get("gpu") if device == "cuda" else d.get("cpu", "unknown")
    notes = "GPU inference" if device == "cuda" else "CPU fallback"
    rows.append(f"| {tag} | {d.get('arch', '?')} | {device} | {hw} | {d.get('fps', '?')} | {notes} |")

if rows:
    with open("BENCHMARKS.md", "a") as f:
        for row in rows:
            f.write(row + "\n")
    print(f"Appended {len(rows)} benchmark row(s)")
else:
    print("No benchmark rows to append")
