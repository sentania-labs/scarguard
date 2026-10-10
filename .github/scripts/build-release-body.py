#!/usr/bin/env python3
"""Build release_body.md from release environment."""
import json
import os
import textwrap

tag = os.environ["TAG"]
owner = os.environ["OWNER"]
orin_bench = os.environ.get("ORIN_BENCH", "")
x86_bench = os.environ.get("X86_BENCH", "")

lines = textwrap.dedent(f"""\
## Docker Images

Pull and run this release:

```bash
# In your .env file, set:
IMAGE_TAG={tag}

docker compose pull
docker compose up -d
```

Or update a running installation:
```bash
echo "IMAGE_TAG={tag}" >> .env
docker compose pull && docker compose up -d
```

## Images

| Service | Image | Platform |
|---------|-------|----------|
| detector (Jetson) | `ghcr.io/{owner}/scarguard-detector:{tag}` | ARM64 / L4T |
| detector (x86) | `ghcr.io/{owner}/scarguard-detector-x86:{tag}` | x86_64 (CUDA + CPU) |
| web | `ghcr.io/{owner}/scarguard-web:{tag}` | amd64, arm64 |
| notifier | `ghcr.io/{owner}/scarguard-notifier:{tag}` | amd64, arm64 |
| deterrent | `ghcr.io/{owner}/scarguard-deterrent:{tag}` | amd64, arm64 |
| backup | `ghcr.io/{owner}/scarguard-backup:{tag}` | amd64, arm64 |
| caddy | `ghcr.io/{owner}/scarguard-caddy:{tag}` | amd64, arm64 |
| log-streamer | `ghcr.io/{owner}/scarguard-log-streamer:{tag}` | amd64, arm64 |
| training-controller | `ghcr.io/{owner}/scarguard-training-controller:{tag}` | amd64, arm64 |
| trainer | `ghcr.io/{owner}/scarguard-trainer:{tag}` | ARM64 / L4T |

## Benchmarks

| Arch | Device | Hardware | FPS | Notes |
|------|--------|----------|-----|-------|""")

for label, raw in [("Orin", orin_bench), ("x86", x86_bench)]:
    if not raw or raw == "null":
        continue
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        continue
    device = d.get("device", "unknown")
    hw = d.get("gpu") if device == "cuda" else d.get("cpu", "unknown")
    notes = "GPU inference" if device == "cuda" else "CPU fallback"
    lines += f"| {d.get('arch', '?')} | {device} | {hw} | {d.get('fps', '?')} | {notes} |"

lines += "\n\nSee [BENCHMARKS.md](BENCHMARKS.md) for full benchmark history.\n"

with open("release_body.md", "w") as f:
    f.write(lines)

print(f"Wrote {len(lines)} bytes to release_body.md")
