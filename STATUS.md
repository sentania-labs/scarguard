# ScarGuard — Current Status

## What's Working (Validated)

- **Detection pipeline:** Detector service loads YOLO model, pulls RTSP frames, runs inference, logs to SQLite, publishes to Redis. Running with basic COCO `bird` class model.
- **Email notifications:** SMTP dispatch with snapshot attachment — tested and confirmed.
- **Discord notifications:** Webhook dispatch with snapshot image — tested and confirmed.
- **Webhook notifications:** Generic HTTP/HTTPS webhook channel (POST or PUT, optional Bearer auth).
- **Named notification channels:** Multi-instance per type (`notifications.channels`), each with a unique name. Legacy flat `discord`/`email` keys still work.
- **Web UI:** Dashboard, event log, config editor (form + raw YAML), model upload — functional.
- **CI/CD:** GitHub Actions workflows build and push images to GHCR. 3 x86 docker runners + 3 generic runners + 1 Orin runner. Web, notifier, caddy, and detector-x86 builds parallelized across docker runners. CI lint/tests run on PRs only; main-push builds warm the GHA cache without re-running tests. Weekly cleanup hits all docker runners via matrix strategy. Compose smoke test, GPU/CPU inference benchmarks in CI.
- **x86 detector:** CUDA+CPU detector image (`scarguard-detector-x86`) runs on any x86 Linux with or without NVIDIA GPU. CPU fallback via PyTorch.
- **Docker Compose stack:** All six services (redis, caddy, detector, web, notifier, log-streamer) start and communicate correctly.
- **Config hot-reload:** Detector and notifier poll config and apply changes in-process (no service restart required).
- **External data directory:** Application assets (config, data, models, snapshots) stored externally to the project repo.
- **Notifier resilience:** Internet interruptions handled with per-notifier retry queue and exponential backoff.
- **Detection exclusion zones:** Per-camera normalized rectangular zones drawn in the config editor canvas; detections inside excluded.
- **Action rules:** Per-camera, per-class channel routing. First-match-wins rules stored in YAML and editable in the config GUI.
- **Enhanced event log:** Filter by camera, class, date range. `actions_triggered` column shows which channels were notified.
- **Scheduled arm/disarm:** Fixed-time schedule (HH:MM) or solar mode (sunrise/sunset) via `astral`; manual dashboard overrides respected until next transition.
- **Live feed:** Detection-triggered annotated snapshot feed with SSE, offline indicator, and exponential-backoff auto-reconnect.
- **App auth:** Session-based login gates all web UI routes. First-run setup page, user management admin UI, API bearer tokens, bcrypt passwords, per-user lockout after N failed attempts.
- **Detection feedback:** Each event can be labeled as Correct, False Positive, or Wrong Class (with corrected class). HTMX-powered inline controls with colored badges. Unreviewed events visually distinct.
- **BBox persistence:** Bounding box coordinates and frame dimensions stored in database per detection. Snapshots saved as clean frames; browser renders bbox overlay from stored data.
- **Training data dashboard:** Admin page showing per-class feedback breakdown, CSS bar charts, exportable event count, date range filter.
- **YOLO dataset export:** One-click zip download with clean images, normalized bbox annotations, and data.yaml. Includes correct and wrong-class (with corrected label) events.
- **Custom model training:** Self-contained `training/train.py` CLI script for fine-tuning YOLO models on exported datasets.
- **Model evaluation:** Admin page for side-by-side model comparison. Runs inference on labeled snapshots via detector's GPU, displays per-class precision/recall/F1, sample detections, and model promotion button.
- **Per-camera detection models:** Each camera can use a different YOLO model and detect different classes. ModelPool manages ref-counted model instances. Falls back to global defaults when per-camera settings are omitted.
- **Named Docker volumes:** All application data (config, models, data, notifier state) stored in named Docker volumes instead of bind mounts. Cleaner deployment, no host-path dependencies.
- **PR image builds:** CI validates Docker image builds on pull requests (not just on merge to main).
- **Ntfy push notifications:** Lightweight push notification channel via ntfy.sh or self-hosted ntfy server. Supports Bearer/Basic auth, configurable priority, snapshot attachment.
- **Visit duration tracking:** Consecutive detections of the same species on the same camera grouped into visit sessions. Visits page with filtering and pagination.
- **Camera health monitoring:** Per-camera online/offline tracking with dashboard indicators and notification alerts when cameras stay offline beyond threshold.
- **Metrics persistence & trending:** System metrics (CPU, GPU, RAM, per-camera FPS) stored in SQLite with historical Chart.js charts, time-range selector, and CSV export.
- **Training data nudge:** Dashboard banner when enough labeled events exist since last dataset export, with per-class breakdown.
- **Config backup & rollback:** Auto-backup on config change with admin UI for listing, diffing, and restoring backups.
- **On-demand camera snapshot:** Dashboard button to grab a live frame from any camera via Redis request/response pattern, even when disarmed.
- **CI/CD hardening:** Container-based pytest, Trivy security scanning, VERSION consistency checks, categorized release notes.
- **Unified data retention:** Single `system.retention_days` config (default 90) drives cleanup of snapshots, events, visits, and metrics. Labeled training data is never pruned. Legacy config keys auto-migrated.
- **Scheduled digest reports:** Configurable daily/weekly/monthly digest via any notification channel. Includes detection summary, visit highlights, performance stoplight, storage usage, and training data stats. Notifier-owned with read-only DB access.
- **Mobile-friendly admin menu:** Admin dropdown works on touch devices (Safari). Nav wraps on small screens.
- **HTML email notifications:** Detection alert emails now use HTML with inline-embedded snapshot images (Content-ID). Plaintext fallback for clients that don't render HTML.
- **One-click notification feedback:** Each detection event generates a one-time feedback token (UUID4). Email, Discord, and ntfy notifications include feedback links/buttons. Standalone confirmation page (no login required, 7-day token expiry).
- **Config UI normal/expert modes:** Toggle switch hides advanced fields (stats intervals, backup settings, TLS, auth, schedule, per-camera model overrides, exclusion zones, action rules). `readForm()` preserves all values regardless of visibility.
- **Docker health checks:** `/health` HTTP endpoint on web service; `/tmp/healthy` touch file for detector and notifier. Compose healthcheck blocks with `start_period` and retry intervals.
- **SSE keepalive:** Event and feed SSE streams emit `: keepalive` comments every 15 seconds to prevent proxy/browser timeouts.
- **Atomic config writes:** `config_store.save()` uses `tempfile.mkstemp` + `os.replace` to prevent partial writes on crash.
- **SQLite indexes:** Indexes on `detection_events` for `timestamp`, `camera_name`, `class_name`, and `feedback` columns.
- **Camera name sanitization:** Snapshot filenames sanitized with `re.sub(r'[^\w\-]', '_', camera_name)` to prevent path traversal.
- **Non-root containers:** All services run as unprivileged `scarguard` user via gosu entrypoint pattern. Volume ownership fixed on first boot.
- **Token-scoped feedback snapshots:** Feedback page serves snapshots via `/feedback/{token}/snapshot` (token-validated, no global `/snapshots` exposure for unauthenticated users).
- **Non-root containers:** All service Dockerfiles run as `scarguard` user (detector adds `video` group for GPU access).
- **Dependency pinning:** All `requirements.txt` files pin exact versions.
- **Log-streamer sidecar:** Dedicated container tails Docker logs and publishes to Redis pub/sub. Web UI subscribes to Redis for admin log streaming — Docker socket no longer mounted in the web container.
- **Redis authentication:** `requirepass` with `REDIS_PASSWORD` env var across all services.
- **FairLock inference scheduling:** FIFO lock prevents camera thread starvation when multiple cameras share a YOLO model.
- **Caddy reverse proxy:** TLS termination, automatic HTTPS via Let's Encrypt or manual certs.

## Known Issues / Buggy

None currently identified.

## Not Yet Built

- Custom-trained heron model (have the tooling now, need labeled data)
- Physical deterrence — actuator service with Tuya WiFi valve control (see [ACTUATION_SPEC.md](ACTUATION_SPEC.md)); blocked on PoC hardware

See [ROADMAP.md](ROADMAP.md) for upcoming work and [ROADMAP_ARCHIVE.md](ROADMAP_ARCHIVE.md) for completed feature history (1–27).

## Completed Work

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Detection engine (RTSP + YOLO + SQLite + Redis) | ✅ Complete |
| 2 | Notifications (Discord webhook + Email SMTP) | ✅ Complete & Validated |
| 3 | Web UI (dashboard, events, config editor, model upload) | ✅ Complete |
| — | CI/CD pipeline (GitHub Actions, GHCR, self-hosted runners) | ✅ Complete |
| — | Docker Compose orchestration + setup.sh installer | ✅ Complete |
| — | External data directory (config/data/models outside repo) | ✅ Complete |
| — | Config hot-reload (detector + notifier poll and apply config in-process) | ✅ Complete |
| — | Form-based config GUI (initial implementation) | ✅ Complete |
| — | Multi-camera detection (initial implementation) | ✅ Complete  |
| — | About page to display version and service status | ✅ Complete |
| v0.3 | Admin logs tab (live Docker log tail via SSE, filterable by service and level) | ✅ Complete |
| v0.3 | SSL/TLS for web UI (self-signed cert generation in setup.sh, HTTP+HTTPS dual-listener) | ✅ Complete |
| v0.3 | Snapshot retention & cleanup (configurable retention_days, daily pruning in detector) | ✅ Complete |
| v0.3.1 | SSL settings widget in config editor, auto-restart on SSL config change | ✅ Complete |
| v0.3.1 | Removed docker-compose.ssl.yml override (HTTPS port now in main compose) | ✅ Complete |
| v0.4 | Detection exclusion zones (per-camera canvas editor, normalized coords, hot-reload) | ✅ Complete |
| v0.4 | Enhanced event log (filter by camera/class/date, actions_triggered column) | ✅ Complete |
| v0.4 | Per-camera action rules (route detections to specific named channels) | ✅ Complete |
| v0.4 | Live feed improvements (offline indicator, exponential-backoff auto-reconnect, XSS-safe SSE) | ✅ Complete |
| v0.4 | Named notification channels & webhook support (multi-instance, named, backward-compat) | ✅ Complete |
| v0.4 | Scheduled arm/disarm (fixed-time + solar mode, manual override, hot-reload) | ✅ Complete |
| v0.5 | GPU/CPU load stats view (live system resource metrics, per-camera inference FPS/latency, mini-charts) | ✅ Complete |
| v0.6 | App security & user accounts (session auth, first-run setup, user management, API tokens, lockout) | ✅ Complete |
| v0.7 | Detection feedback & bbox persistence (per-event labeling, clean snapshots, browser bbox overlay) | ✅ Complete |
| v0.7 | Training data dashboard & YOLO dataset export (admin page, bar charts, zip export) | ✅ Complete |
| v0.7 | Custom model training script (training/train.py CLI, dataset validation, Jetson tips) | ✅ Complete |
| v0.7 | Model evaluation in web UI (side-by-side comparison, SSE progress, model promotion) | ✅ Complete |
| v0.8 | Per-camera detection models, classes & action routing (ModelPool, per-camera config, hot-reload) | ✅ Complete |
| v0.8 | Named Docker volumes (replace bind mounts, migration script, setup.sh rewrite) | ✅ Complete |
| v0.8 | CI: PR image build validation (build.yml triggers on pull_request) | ✅ Complete |
| v0.9 | Ntfy push notifications (new channel type, Bearer/Basic auth, priority, snapshot) | ✅ Complete |
| v0.9 | Visit duration tracking (session grouping, SQLite persistence, Visits page) | ✅ Complete |
| v0.9 | Camera health monitoring & alerts (online/offline tracking, dashboard indicators, notifications) | ✅ Complete |
| v0.9 | Metrics persistence & historical trending (SQLite, Chart.js, CSV export) | ✅ Complete |
| v0.9 | Training data readiness nudge (dashboard banner, app_state table) | ✅ Complete |
| v0.9 | Config backup & rollback (auto-backup, admin UI, diff/restore) | ✅ Complete |
| v0.9 | On-demand camera snapshot (Redis request/response, dashboard button) | ✅ Complete |
| v0.9 | CI/CD hardening (container pytest, Trivy scanning, VERSION checks, release notes) | ✅ Complete |
| v0.10 | CI/CD pipeline hardening (compose smoke test, GPU/CPU inference benchmarks, BENCHMARKS.md) | ✅ Complete |
| v0.10 | x86/CUDA detector image (Dockerfile.x86, CPU fallback, setup.sh platform detection) | ✅ Complete |
| v0.11 | Unified retention, digest reports, mobile menu, stats chart fix, event pruning | ✅ Complete |
| v0.12 | HTML email, notification feedback tokens, config UI modes, health checks, Caddy TLS proxy | ✅ Complete (Beta 1) |
| v0.12.3 | Hardening: base image pins, CI alignment, graceful shutdown, GPU release, frame skip, SSE backpressure, Redis buffering | ✅ Complete |
| v0.12.4 | Hardening: Redis auth, ConfigWatcher dedup, AtomicRef thread safety, live feed removal | ✅ Complete |
| v0.12.5 | Hardening: run_camera refactor, FairLock inference fairness, stats chart fix, setup.sh upgrade UX | ✅ Complete |
| v0.12.6 | Hardening: log-streamer sidecar, Docker socket removal from web container | ✅ Complete |
| v0.12.7 | Hotfix: inference time regression — pin ultralytics save_dir to `/tmp/runs/predict` with `exist_ok=True`.  v0.12.1-v0.12.6 passed `project="/tmp/runs"` to `model.predict()` which triggered ultralytics' `increment_path` to create a new `predict{N}` directory per call and stat every existing one on the next call.  After ~24 h of sustained inference the directory count reached 9998 and each predict call was spending 45-48% of its wall time in `os.path.exists()`.  Fix restores v0.11.0 performance (~50 ms per call).  See `INFERENCE_INVESTIGATION.md`. | 🚧 In progress |
