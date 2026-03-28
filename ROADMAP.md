# ScarGuard — Roadmap

Active and planned features. Each item includes acceptance criteria. Completed features (1–15, 18–22, 24–25) are in [ROADMAP_ARCHIVE.md](ROADMAP_ARCHIVE.md).

---

## Feature 16: CI/CD Pipeline Hardening (Gate 1 complete, Gate 2 mostly complete — v0.9)

Three-gate CI strategy: lint on push, build + test on PR, push on release.

**Gate 1 — On push to any branch:** ✓ Complete
- ✓ Ruff lint across all services
- ✓ mypy type checking (web + notifier)
- ✓ pytest (web + notifier)
- Fast feedback, no image builds

**Gate 2 — On PR to main:** Mostly complete
- ✓ Build all three service images (web, notifier on x86; detector on Orin runner) — images not pushed
- ✓ Run pytest *inside* the built containers (not just against source)
- ✓ Trivy container image scanning (CRITICAL/HIGH severity)
- ✓ VERSION file consistency check
- Smoke test: `docker compose up`, verify Redis health, web UI `/health` endpoint responds, detector logs "Model loaded" (Orin runner only)
- PR cannot merge if any gate fails

**Gate 3 — On tag push:**
- ✓ Build and push images to GHCR (same as today)
- ✓ Validate VERSION matches tag
- ✓ Categorized auto-generated release notes
- Optionally: auto-deploy to Orin via SSH or webhook

**Acceptance criteria:**
- ✓ Ruff + mypy run on every branch push, fail the check on violations
- ✓ PR workflow builds all three images (not pushed to GHCR)
- ✓ PR workflow runs pytest inside each built container
- ✓ PR workflow runs Trivy security scanning on built images
- ✓ VERSION file validated on every CI run and matched against release tag
- PR workflow runs a compose smoke test (stack up, health check, stack down)
- Detector smoke test runs on Orin runner with GPU (model load + single frame inference)
- ✓ Tag workflow builds and pushes to GHCR (existing behavior, unchanged)

---

## Feature 17: x86/CUDA Detector Image

Build an x86-compatible detector container so ScarGuard can run on non-Jetson hardware. The current detector image is ARM64/L4T only. This removes the "must own a Jetson" barrier for adoption.

**Acceptance criteria:**
- x86/CUDA detector Dockerfile (e.g. based on `pytorch/pytorch` or `nvidia/cuda` + `ultralytics`) builds and runs inference successfully
- x86 detector image published to GHCR alongside ARM64 image (multi-arch manifest or separate tag)
- CI builds x86 detector on existing x86 runners (no Orin required)
- `docker-compose.yml` works on both Jetson and x86 without manual edits (auto-detect or env var)
- TensorRT `.engine` files are architecture-specific — document that `.pt` models work cross-platform but `.engine` must be regenerated per-device
- Stats collector works on x86 (nvidia-smi path already implemented)
- CPU-only inference as stretch goal: detector runs without NVIDIA GPU using PyTorch CPU backend. Slower but functional with small models (yolov8n). Requires `runtime: nvidia` to be optional in compose.
- `setup.sh` detects host architecture and pulls the correct image
- README updated with x86 deployment instructions alongside Jetson

---

## Feature 23: Scheduled Summary Reports

Daily or weekly email digest summarizing detection activity — the "check over coffee" report.

**Acceptance criteria:**
- Configurable schedule: daily, weekly, or disabled (default: disabled)
- Config section: `system.summary_report` with `enabled`, `frequency` (daily/weekly), `channels` (list of notification channel names to send the report to — email and/or Discord)
- Report contents: total detections by class, detections by camera, longest visit duration (if Feature 19 is implemented), camera uptime summary (if Feature 20 is implemented), system health snapshot (avg CPU/GPU usage)
- Report covers the previous day (daily) or previous 7 days (weekly)
- For email channels: formatted HTML email with inline summary table
- For Discord channels: formatted embed message
- Report generation runs as a scheduled task in the notifier or web service
- Configurable via web UI Settings

---

## Feature 26: Event Record Pruning & Retention

Extend snapshot retention to also prune old detection event records from SQLite. Prevents unbounded DB growth. Coordinate with the training pipeline to protect labeled data.

**Acceptance criteria:**
- Change default `system.snapshot_retention_days` from 30 to 90 (training dataset collection needs a longer window)
- New config field: `system.event_retention_days` (default: matches `snapshot_retention_days`)
- Pruning deletes event records older than retention period from `detection_events` table
- **Protected events:** Events with feedback labels (`correct`, `false_positive`, `wrong_class`) are NEVER pruned — they are training data. Only unlabeled events are eligible for pruning.
- Pruning runs on the same daily schedule as snapshot cleanup
- Visit records (Feature 19) follow the same retention policy
- Metrics data (Feature 21) has its own independent retention (`metrics_retention_days`)
- Dashboard or training page shows count of protected (labeled) vs pruneable events
- `event_retention_days: 0` disables event pruning (keep forever, matching snapshot behavior)

---

## Cleanup / Deprecation

- **Remove legacy SSL→TLS migration** (target v1.0 or v0.12) — `_migrate_ssl_to_tls()` in `main.py` and the legacy `ssl:` fallback in `caddy-entrypoint.sh`. Added in v0.9 to support users upgrading from the old `ssl:` config section. Safe to remove once enough releases have passed.

---

## Future Ideas (Unprioritized)

- Twilio SMS notifications — paid per-message, but works on any phone without an app
- Per-class cooldown — different cooldown values per detected species (e.g. 30s for squirrels, 5min for herons)
- Mobile-friendly layout — responsive CSS for phone/tablet use; blocked on solving secure external access (reverse proxy, Tailscale, Cloudflare Tunnel)
- UI polish — logo, favicon, login screen branding. Japanese-inspired koi aesthetic. Use AI image generation for assets. Lowest priority.
- ONVIF camera auto-discovery — scan local network for RTSP cameras instead of manual URL entry. Helps non-UniFi users.
- Home Assistant MQTT discovery — auto-register ScarGuard as an HA device, beyond generic webhook
- Confidence auto-tuning — analyze feedback data to suggest optimal confidence thresholds per class
- Night/IR mode awareness — detect when cameras switch to IR and adjust confidence thresholds or flag detections accordingly
- NVR-lite — proxy live RTSP video + audio through the web UI (HLS or WebRTC). Significant scope; dedicated NVR tools (Frigate, Protect) already do this well.
- S3/Minio remote config backup — upload config backups to object storage for off-device redundancy
- Automated Orin runner/self-updates — CI pushes runner updates to Orin via SSH (parked)
