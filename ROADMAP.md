# ScarGuard — Roadmap

Active and planned features. Each item includes acceptance criteria. Completed features (1–15) are in [ROADMAP_ARCHIVE.md](ROADMAP_ARCHIVE.md).

---

## Feature 16: CI/CD Pipeline Hardening (Gate 1 complete, Gate 2 partial — v0.8)

Three-gate CI strategy: lint on push, build + test on PR, push on release.

**Gate 1 — On push to any branch:** ✓ Complete
- ✓ Ruff lint across all services
- ✓ mypy type checking (web + notifier)
- ✓ pytest (web + notifier)
- Fast feedback, no image builds

**Gate 2 — On PR to main:** Partial
- ✓ Build all three service images (web, notifier on x86; detector on Orin runner) — images not pushed
- Run pytest *inside* the built containers (not just against source)
- Smoke test: `docker compose up`, verify Redis health, web UI `/health` endpoint responds, detector logs "Model loaded" (Orin runner only)
- PR cannot merge if any gate fails

**Gate 3 — On tag push:**
- Build and push images to GHCR (same as today)
- Optionally: auto-deploy to Orin via SSH or webhook

**Acceptance criteria:**
- ✓ Ruff + mypy run on every branch push, fail the check on violations
- ✓ PR workflow builds all three images (not pushed to GHCR)
- PR workflow runs pytest inside each built container
- PR workflow runs a compose smoke test (stack up, health check, stack down)
- Detector smoke test runs on Orin runner with GPU (model load + single frame inference)
- Tag workflow builds and pushes to GHCR (existing behavior, unchanged)

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

## Feature 18: Ntfy Push Notifications

First-class notification channel type for [Ntfy](https://ntfy.sh) — lightweight push notifications to phone/desktop without running a separate app ecosystem.

**Acceptance criteria:**
- New channel type `ntfy` alongside existing `discord`, `email`, `webhook`
- Config: `name`, `type: ntfy`, `topic`, `server` (default: `https://ntfy.sh`), `enabled`, optional `token` for authenticated topics
- Snapshot attached as image (Ntfy supports image attachments via URL or base64)
- Priority mapping: configurable default priority (1–5), optional per-class priority overrides (e.g. heron = urgent/5, bird = low/2)
- Action buttons in notification: "View Event" link back to ScarGuard web UI event page
- Works with self-hosted Ntfy server or the free public `ntfy.sh` instance
- Channel configurable via web UI Settings → Notification Channels (same as Discord/email/webhook)
- Retry with exponential backoff on failure (consistent with other channel types)

---

## Feature 19: Visit Duration Tracking

Track how long a detected species stays in view per camera. Measures time from first detection to last detection of the same species on the same camera within a session window. Provides a baseline metric; later, when Scar's Revenge is active, enables comparing "visit duration with deterrent" vs "without."

**Acceptance criteria:**
- Define a "visit session" as consecutive detections of the same class on the same camera with no gap longer than a configurable timeout (default: 5 minutes)
- When a session ends (gap exceeded or species changes), write a visit record: camera, class, start time, end time, duration, detection count
- Visit records stored in SQLite (new table or extension of detection_events)
- Visit duration visible on the Events page — either inline on detection rows or as a separate "Visits" view
- Per-species, per-camera visit statistics: average duration, longest visit, visit count (queryable by date range)
- Historical charting: visit duration over time, filterable by species and camera
- Data persisted and available for the metrics/trending feature (Feature 21)

---

## Feature 20: Camera Health Monitoring & Alerts

Track camera uptime and notify when a camera stays offline. The detector already reconnects silently on RTSP drop — this adds visibility and alerting so users know a camera died before the heron shows up.

**Acceptance criteria:**
- Track per-camera: online/offline state, last frame received timestamp, reconnect count, time-to-reconnect
- Camera health metrics included in the stats pipeline (alongside CPU/GPU/FPS data) for persistence and trending
- Configurable alert threshold: notify if a camera has been offline for N minutes (default: 10)
- Alerts sent through existing notification channels (same routing as detection alerts — configurable via action rules or a separate `system.camera_health` config section)
- Camera health status visible on the dashboard: green/yellow/red indicator per camera
- Health history: uptime percentage, reconnect events, and downtime windows queryable by date range
- No false alerts on brief RTSP hiccups (debounce — only alert after sustained downtime exceeding threshold)

---

## Feature 21: Metrics Persistence & Historical Trending

Store system and detection metrics over time instead of displaying live-only. Enables historical charts, trend analysis, and correlation with visit duration and camera health data.

**Acceptance criteria:**
- Persist metrics to SQLite (new table): CPU usage, GPU usage, GPU temp, RAM, per-camera inference FPS, per-camera inference latency
- Sampling interval matches `system.stats_interval` (default: 5s) — store every Nth sample to control DB growth (e.g. one row per minute for long-term, full resolution for last hour)
- Configurable retention: `system.metrics_retention_days` (default: 90)
- Admin page or enhanced Stats page with time-range selector: last hour, last 24h, last 7d, last 30d, custom range
- Charts: CPU/GPU usage over time, inference FPS per camera, latency trends, temperature
- Camera health metrics (Feature 20) and visit duration data (Feature 19) surfaced in the same trending UI
- Metric pruning runs on same schedule as snapshot cleanup (daily)
- Export metrics as CSV for external analysis (optional stretch)

---

## Feature 22: Training Data Readiness Nudge

Dashboard banner that lets users know when they've accumulated enough labeled data to consider training or retraining a model. Guidance-oriented, not automated.

**Acceptance criteria:**
- Banner on the dashboard (dismissible) when new labeled events since last dataset export exceed a configurable threshold (default: 100 confirmed positives)
- Banner includes: count of new labels by class since last export, date of last export
- Guidance text: suggest supplementing sparse classes with external datasets (iNaturalist, Macaulay Library) for better model generalization
- Link to the Training Data admin page from the banner
- Track "last export date" in SQLite or config so the nudge resets after each export
- No automated training — this is purely informational

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

## Feature 24: Config Backup & Rollback

Automatically back up `scarguard.yml` on a schedule and on changes. Local retention with rollback capability so users can recover from accidental config damage via the web UI.

**Acceptance criteria:**
- **Scheduled backup:** Daily timestamped copy of `scarguard.yml` stored in a `backups/` directory inside the config volume
- **Change-triggered backup:** When config is saved (via web UI or file change), queue a backup. Debounce with a 3-minute window — if multiple saves happen within 3 minutes, only one backup is created
- **Retention:** Configurable number of backup copies to retain (default: 30). Oldest pruned when limit is exceeded. At minimum, retain 1 day of backups so a same-day rollback is always possible
- **Rollback via UI:** Admin page listing available backups with timestamps and diff preview. "Restore" button replaces current config with selected backup and triggers hot-reload
- **Rollback via API:** REST endpoint to list backups and trigger restore (for scripted recovery)
- **Backup naming:** `scarguard.yml.YYYYMMDD-HHMMSS.bak` — human-readable, sortable
- Config section: `system.backup` with `enabled` (default: true), `max_copies` (default: 30), `backup_on_change` (default: true)

---

## Feature 25: On-Demand Camera Snapshot

Pull a live frame from any configured camera on demand — "what does the pond look like right now?" without waiting for a detection event.

**Acceptance criteria:**
- Web UI: "Snapshot" button on the dashboard or camera list that captures and displays a current frame from the selected camera
- Snapshot is a single RTSP frame grab via OpenCV (same mechanism the detector uses), not a continuous stream
- Option to share the snapshot: send to a notification channel (Discord, email, Ntfy) directly from the UI
- Snapshot is temporary — not persisted in the detection events table or snapshot directory (unless the user explicitly saves it)
- Works even when the system is disarmed (camera threads may or may not be active — may need a dedicated frame grab)
- API endpoint for programmatic snapshot requests (Bearer auth): `GET /snapshot/{camera_name}` returns JPEG
- Graceful error if camera is offline: show last-known frame or clear error message

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
