# ScarGuard — Roadmap

Current priorities in order. Each item includes acceptance criteria.

---

## ~~Feature 1: Admin Logs Tab~~ ✓ Complete (v0.3)

Add a "Logs" tab under admin/configuration section of the web UI.

**Acceptance criteria:**
- ✓ Accessible from web UI navigation (e.g. under "Admin" or "System" dropdown)
- ✓ Displays recent log output from each service: detector, notifier, web
- ✓ Logs sourced from Docker container logs via Docker socket or from shared volume log files
- ✓ Filterable by service name and log level (info, warning, error)
- ✓ Auto-scroll / tail mode with pause option
- ✓ Reasonable log buffer (last N lines or last N minutes, configurable)

**Implementation notes:**
- Docker SDK streams container logs via `/var/run/docker.sock` (mounted read-only into web container)
- SSE endpoint at `/admin/logs/stream`; CSS-driven level filtering avoids reconnect on filter change
- `docker` Python package added to `services/web/requirements.txt`
- Security: Docker socket = host-root equivalent access; must be gated by Feature 9 (auth) before exposing beyond LAN

---

## ~~Feature 2: SSL / TLS for Web UI~~ ✓ Complete (v0.3)

Support both HTTP and HTTPS. Self-signed cert by default, option for custom cert.

**Acceptance criteria:**
- ✓ `setup.sh` generates a self-signed cert if none exists at the configured path
- ✓ Config in `scarguard.yml` specifies cert/key paths and whether HTTPS is enabled
- ✓ Web service listens on both HTTP (8080) and HTTPS (8443), or HTTPS-only if configured
- ✓ User can drop in their own cert/key and restart to use it

**Implementation notes:**
- `services/web/src/start.py` replaces direct `uvicorn` CMD; reads `ssl` section from `scarguard.yml` at boot
- HTTP (8080) daemon thread + HTTPS (8443) main thread; `https_only: true` skips HTTP listener
- `ssl.keyfile_password` supported for passphrase-protected private keys
- Certs directory (`${DATA_DIR}/config/certs`) bind-mounted into web container as `/certs:ro`
- HTTPS port `${WEB_HTTPS_PORT:-8443}:8443` bound in `docker-compose.yml` alongside HTTP
- SSL settings widget in the web UI config editor (v0.3.1)
- Config watcher thread in `start.py` detects SSL changes and triggers automatic web service restart (v0.3.1)

---

## ~~Feature 3: Snapshot Retention & Cleanup~~ ✓ Complete (v0.3)

Snapshots accumulate indefinitely. Add configurable retention policy.

**Acceptance criteria:**
- ✓ Config field: `system.snapshot_retention_days` (default: 30)
- ✓ Background task prunes snapshots older than retention period
- ✓ Corresponding SQLite records cleaned up or marked as snapshot-expired
- ✓ Runs on schedule (daily) and at startup

**Implementation notes:**
- `services/detector/src/cleanup.py`: `SnapshotCleaner` daemon thread; runs at startup then every 24h
- Deletes snapshot files on disk; NULLs `snapshot_path` in SQLite for pruned rows
- Uses `sqlite3.connect(timeout=30)` to handle rare WAL contention with `EventProcessor`
- Set `snapshot_retention_days: 0` to disable retention (keep forever)

---

## ~~Feature 4: Detection Exclusion Zones~~ ✓ Complete (v0.4)

Suppress false positives from static objects (e.g. heron decoy). Two tiers.

**Tier 1 — Manual exclusion zones (implement now):**
Per-camera rectangular mask regions drawn in the web UI. Detection whose bounding box center falls inside an exclusion zone is silently dropped. Zones saved in `scarguard.yml` under the camera entry.

**Acceptance criteria (Tier 1):**
- ✓ Web UI overlay on camera snapshot lets user draw/resize/delete rectangular exclusion zones
- ✓ Zones stored per-camera: `cameras[].exclusion_zones: [{x, y, w, h, label}]`
- ✓ Detector checks every detection against zones before publishing events
- ✓ Label is optional (e.g. "heron decoy")
- ✓ Zones survive config reload and service restart

**Tier 2 — Automatic static object detection (future/stretch):**
Track detections that remain in the same position across many frames over hours/days. If an object hasn't moved beyond threshold, prompt user in web UI to add exclusion zone. Never auto-exclude without user approval.

---

## ~~Feature 5: Enhanced Detection Event Logs~~ ✓ Complete (v0.4)

Richer detail and filtering in the web UI event log.

**Acceptance criteria:**
- ✓ Each event shows: timestamp, camera name, detected class, confidence, actions triggered
- ✓ Filter/sort by camera, detected class, date range
- ✓ Per-class or per-camera action rules in config (e.g. "bird → notify only", "heron → notify + valve")
- ✓ Action rules configurable in `scarguard.yml` and GUI config editor

---

## ~~Feature 6: Live Camera Feed in Web UI~~ ✓ Complete (v0.4)

SSE-based detection-triggered annotated snapshot feed with bounding boxes.

**Acceptance criteria:**
- ✓ Camera feed visible at `/feed` (detection-triggered annotated snapshots with bounding boxes)
- ✓ Bounding boxes drawn on detected objects (annotated by detector using OpenCV)
- ✓ Graceful degradation on stream drop (shows "offline" indicator, auto-reconnects with exponential backoff)

---

## ~~Feature 7: Named Notification Channels & Webhook Support~~ ✓ Complete (v0.4)

Refactor notifications from single-instance-per-type to named, multi-instance channels. Add webhook as a new channel type. Each channel gets a unique name that action rules reference — just like cameras.

**Example config:**
```yaml
notifications:
  channels:
    - name: pond-alerts
      type: discord
      enabled: true
      webhook_url: https://discord.com/api/webhooks/...pond-channel...

    - name: security-alerts
      type: discord
      enabled: true
      webhook_url: https://discord.com/api/webhooks/...security-channel...

    - name: email-digest
      type: email
      enabled: true
      smtp_host: smtp.gmail.com
      recipients: [scott@example.com]

    - name: heron-deterrent
      type: webhook
      enabled: true
      url: http://192.168.1.50/api/spray
      method: POST
      headers: { "Authorization": "Bearer ..." }

    - name: home-assistant
      type: webhook
      enabled: true
      url: http://homeassistant.local:8123/api/webhook/scarguard
      method: POST
```

**Acceptance criteria:**
- ✓ Every notification channel has a unique `name` and a `type` (discord, email, webhook)
- ✓ Multiple instances of the same type supported (e.g. two Discord webhooks to different channels)
- ✓ Webhook type is configurable: URL, HTTP method (POST/PUT), optional auth token
- ✓ Webhook payload includes: event timestamp, camera, detected class, confidence, snapshot filename
- ✓ Retry with backoff on failure (all channel types)
- ✓ Action rules reference channels by name (e.g. `channels: [pond-alerts, heron-deterrent]`)
- ✓ Web UI config editor supports add/remove/edit of named channels
- ✓ Backward compatibility: legacy `notifications.discord` / `notifications.email` keys still work

---

## ~~Feature 8: Scheduled Arm/Disarm~~ ✓ Complete (v0.4)

Automatically arm and disarm the detection system on a daily schedule. Primary use case: arm at dawn when herons hunt, disarm at dusk when activity is expected around the pond. Eliminates daily manual toggling of `system.armed`.

**Acceptance criteria:**
- ✓ Config fields: `system.schedule.arm_time` and `system.schedule.disarm_time` (24h format, e.g. `"06:00"`, `"20:30"`)
- ✓ Optional: `system.schedule.use_solar` — calculates sunrise/sunset from `latitude`/`longitude` via `astral`
- ✓ Scheduler runs inside the detector service, checks every 60 seconds
- ✓ Manual arm/disarm via UI overrides the schedule until the next scheduled transition
- ✓ Arm/disarm transitions logged as system events visible in the event log
- ✓ If no schedule configured, behavior unchanged (manual only)
- ✓ Schedule status and next transition time visible on the dashboard

---

## Feature 9: App Security & User Accounts

Add authentication to the web UI. Currently anyone on the network can access the dashboard, config, and admin tools.

**Acceptance criteria:**
- Login page gates all web UI routes — no unauthenticated access to dashboard, config, admin, or API endpoints
- At least one admin user created during `setup.sh` (prompted for username/password)
- Passwords hashed (bcrypt or argon2), stored in SQLite — never plaintext
- Session-based auth with configurable timeout (default: 24h)
- User management in admin UI: add/remove users, change passwords
- API endpoints (webhook callbacks, SSE feeds) support token-based auth as alternative to session cookies
- Rate-limit or lockout after N failed login attempts (default: 5 attempts, 15 min lockout)
- Works over both HTTP and HTTPS (log a warning on startup if auth is enabled without TLS)

**Upgrade path for existing installations:**
- If no users exist in the database on startup (i.e. pre-auth install upgraded via `docker pull`), the app starts in a **first-run setup mode**: web UI redirects to a one-time account creation page before anything else is accessible
- No default/hardcoded credentials — the user must set their own on first launch
- Existing API integrations (webhooks, SSE) continue to work unauthenticated until the user explicitly enables API token auth via config (`system.require_api_auth: true`, default `false`)
- Migration is non-destructive: pulling the new image and restarting is all that's needed

---

## Feature 10: Per-Camera Detection Models, Classes & Action Routing

Allow each camera to use a different YOLO model, detect different object classes, and route detections to specific notification channels. This is the core of multi-camera/multi-model setups.

**Example config:**
```yaml
cameras:
  - name: pond-north
    rtsp_url: rtsp://...
    model_path: models/heron-v1.pt
    detect_classes: [great_blue_heron, green_heron]
    action_rules:
      - classes: [great_blue_heron, green_heron]
        actions: [pond-alerts, heron-deterrent]   # Discord + spray valve

  - name: front-door
    rtsp_url: rtsp://...
    # model_path omitted — falls back to global detection.model_path
    detect_classes: [person, car]
    action_rules:
      - classes: [person]
        actions: [security-alerts]                 # different Discord channel
      - classes: [car]
        actions: []                                # log only

  - name: pond-south
    rtsp_url: rtsp://...
    model_path: models/heron-v1.pt
    detect_classes: [person, great_blue_heron]
    action_rules:
      - classes: [person]
        actions: []                                # humans at pond = do nothing
      - classes: [great_blue_heron]
        actions: [heron-deterrent]                 # spray only, no notification
```

**Acceptance criteria:**
- Camera config gains optional fields: `model_path`, `detect_classes`, and `action_rules`
- If `model_path` or `detect_classes` omitted, camera falls back to global `detection.model_path` / `detection.classes`
- `action_rules` is a list of `{classes: [...], actions: [...]}` pairs — matched top-down, first match wins
- Actions reference notification channels by name (as defined in Feature 7), not by type
- Validate at startup that all channel names referenced in action rules exist; log a warning for unresolved references
- If no `action_rules` defined on a camera, falls back to global notification behavior (all enabled channels)
- Detector loads each unique model once in memory and shares across cameras using the same model
- Model swap per-camera via the web UI config editor (dropdown of uploaded models)
- Hot-reload: changing a camera's model, classes, or action rules takes effect without restarting the stack
- Validate at startup that referenced model files exist; log a clear error and skip the camera if not
- Web UI config editor surfaces per-camera model/class/action-rule editing with a usable UI (not raw YAML)
- Detection events include which actions were triggered (or "log only") for traceability in the event log

---

## ~~Feature 11: GPU/CPU Load Stats View~~ ✓ Complete (v0.5)

System health panel in the web UI showing resource utilization of the host. Useful for tuning inference intervals, monitoring thermal throttling on the Orin, and knowing when hardware limits are hit.

**Acceptance criteria:**
- Dashboard widget or dedicated admin page showing: CPU usage (%), GPU usage (%), GPU memory (used/total), system RAM (used/total), CPU temperature, GPU temperature
- GPU stats sourced from `jtop`/`tegrastats` (Jetson) or `nvidia-smi` (x86) — auto-detect platform
- Updates on a polling interval (configurable, default: 5s)
- Graceful degradation: if no NVIDIA GPU detected, show CPU-only stats without errors
- Per-camera inference FPS and average inference latency displayed alongside resource stats
- Historical mini-chart (last 5–10 minutes) for GPU/CPU usage so user can spot trends and throttling

**Implementation notes:**
- Detector service runs a `StatsCollector` daemon thread that reads CPU/RAM from `/proc`, temperatures from `/sys/class/thermal`, and GPU stats from Jetson sysfs or `nvidia-smi` (auto-detected)
- Stats written to Redis key `scarguard:stats` with TTL; web service polls the key via SSE at `/admin/stats/stream`
- Per-camera inference FPS and latency tracked in the detector main loop and included in the stats snapshot
- Mini-charts rendered with inline Canvas JS (no external charting library); rolling 10-minute buffer in the browser
- Config: `system.stats_interval` (1–60 seconds, default 5)

---

## Feature 12: Detection Feedback & Dataset Collection

Add a feedback mechanism to each detection event so confirmed positives and false positives can be labeled in-app. This is the foundation of the model improvement pipeline (Features 13–15).

**Acceptance criteria:**
- Each detection event in the web UI has a feedback control: **Correct**, **False Positive**, **Wrong Class** (with optional corrected class dropdown)
- Feedback written to SQLite: `feedback` column on the events table (`correct` / `false_positive` / `wrong_class`), plus `corrected_class` when applicable
- Feedback can be changed after initial submission
- Unfeedback'd events are visually distinct from reviewed ones in the event log (e.g. badge or row highlight)
- No feedback is required — the system continues to function normally without it; this is purely additive

---

## Feature 13: Dataset Quality Dashboard & Export

Give visibility into the labeled dataset being built from feedback, and provide a one-click export for use in model training.

**Acceptance criteria:**
- Admin page (e.g. "Training Data") showing:
  - Count of confirmed positives per class
  - Count of false positives per class
  - Count of wrong-class corrections
  - Date range coverage of the dataset
  - Simple bar chart of class distribution so gaps are obvious at a glance
- Export button generates a YOLO-format dataset zip: annotated images + per-image `.txt` files (class index + bounding box), plus a `data.yaml` describing classes and splits
- Export respects a date range filter (e.g. export only the last 90 days)
- Export only includes events with `feedback = correct` — false positives and wrong-class events are excluded from the positive set
- Export is downloadable directly from the browser

---

## Feature 14: Custom Model Training

Replace the generic COCO bird model with a fine-tuned model that distinguishes heron species from pond camera imagery. Training is run manually via a committed script; this is not automated.

**Acceptance criteria:**
- `training/train.py` script committed to repo — takes an exported dataset (from Feature 13), fine-tunes a YOLOv8 (or current best) checkpoint, validates, and writes a `.pt` to a configurable output path
- Script is self-contained: all hyperparameters (epochs, image size, batch size, patience) are configurable via CLI args with sensible defaults
- Training dataset: minimum 500 labeled images per target species, sourced from pond cameras and supplemented with public datasets (iNaturalist, Macaulay Library) as needed
- Annotations in YOLO format (one `.txt` per image, class + bounding box)
- Validation mAP@0.5 ≥ 0.75 on a held-out test set of pond camera images
- Species classes at minimum: `great_blue_heron`, `green_heron` — additional species as data allows
- Training notebook or equivalent committed under `training/` for reproducibility
- **Model promotion is always manual** — the script produces a `.pt` file; the user places it in `models/` and selects it in config or the web UI. No automated deployment of trained models.

---

## Feature 15: Model Evaluation in Web UI

Before promoting a newly trained model, compare it against the current one using stored snapshots. Prevents deploying a regression.

**Acceptance criteria:**
- Admin page lets user select two models (current active + a candidate from `models/`) and a date range of stored snapshots to evaluate against
- Runs both models against the selected snapshots and displays side-by-side: precision, recall, mAP@0.5, and a sample of detections from each
- Results are not persisted — this is an interactive comparison tool, not a benchmark database
- Evaluation runs on-device (Jetson GPU); show a progress indicator for long runs
- User can promote the candidate model directly from this page (updates `scarguard.yml` and triggers hot-reload per Feature 10)
- Graceful handling of snapshots where the original annotated bounding box is unavailable (skip or flag)

---

## Feature 16: CI/CD Pipeline Hardening

Current CI builds images on merge to main and pushes on tag, but PRs have no image validation. Add a three-gate CI strategy: lint on push, build + test on PR, push on release.

**Gate 1 — On push to any branch:**
- Lint (ruff/flake8) and type check (mypy) across all services
- Fast feedback, no image builds

**Gate 2 — On PR to main:**
- Build all three service images (web, notifier on x86; detector on Orin runner)
- Run pytest *inside* the built containers (not just against source)
- Smoke test: `docker compose up`, verify Redis health, web UI `/health` endpoint responds, detector logs "Model loaded" (Orin runner only)
- Images are disposable — not pushed to GHCR
- PR cannot merge if any gate fails

**Gate 3 — On tag push:**
- Build and push images to GHCR (same as today)
- Optionally: auto-deploy to Orin via SSH or webhook

**Acceptance criteria:**
- Ruff + mypy run on every branch push, fail the check on violations
- PR workflow builds all three images and runs pytest inside each container
- PR workflow runs a compose smoke test (stack up, health check, stack down)
- Detector smoke test runs on Orin runner with GPU (model load + single frame inference)
- Tag workflow builds and pushes to GHCR (existing behavior, unchanged)

---

## Future Ideas (Unprioritized)

- SMS/iMessage notifications
- Multi-model support (seasonal species profiles)
- Add some splash to the interface, logo on the login screen, favicon
- Automated retraining trigger — a scheduled job that checks "N new confirmed positives since last training run?" and queues a training run; model promotion always remains manual
- Deterrence effectiveness tracking — log how long after a valve fires the animal leaves (based on next detection timestamp), gives you data on what's actually working
- Detection heatmap overlay — where on the camera frame do detections cluster? Useful for tuning detection zones and understanding animal behavior
- Live stats widget — detections this week, most active camera, last detection time, most frequent species. Simple SQLite queries, satisfying dashboard data
- Mobile-friendly layout
- Prometheus metrics endpoint + Grafana — expose inference latency, detection counts, stream reconnect events as metrics
- Automated Orin runner updates via SSH from x86 runners