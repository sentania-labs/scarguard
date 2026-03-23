# ScarGuard — AI-Powered Pond Wildlife Deterrent

## Project Overview

ScarGuard is a containerized wildlife detection and deterrent system that runs on an NVIDIA Jetson Orin Nano. It watches RTSP camera feeds for target species (primarily great blue herons, with expansion to ducks, raccoons, etc.) and triggers randomized deterrent actions (sprinkler valves, notifications) to protect a backyard koi pond.

Named after Scar (aka Kroger), a survivor fish who was badly injured by a heron and lived to tell the tale.

---

## Current Status

### What's Working (Validated)

- **Detection pipeline:** Detector service loads a YOLO model, pulls RTSP frames, runs inference, logs to SQLite, publishes to Redis. Running with a basic COCO `bird` class model.
- **Email notifications:** SMTP dispatch with snapshot attachment — tested and confirmed working.
- **Discord notifications:** Webhook dispatch with snapshot image — tested and confirmed working.
- **Web UI:** Dashboard, event log, config editor, model upload — functional.
- **CI/CD:** GitHub Actions workflow builds and pushes images to GHCR. x86 and Orin self-hosted runners operational.
- **Docker Compose stack:** All four services (redis, detector, web, notifier) start and communicate correctly.
- **Config hot-reload:** Notifier and detector services automatically restart upon config change.
- **External data directory:** Application assets (config, data, models, snapshots) are stored externally to the project repo.

### Implemented but Not Fully Vetted / Buggy

- **Form-based config GUI:** The enable/disable toggle for features/cameras is not visually aligned properly.
- **Form-based config GUI:** Existing cameras are not correctly loaded from the YAML — possible misaligned config structure.
- **Multi-camera simultaneous detection:** Multiple cameras loaded and monitored, but in test setup one camera seems to be preferred over the other. Physical camera stream validation still needed.
- **Notifications:** Internet interruptions are not handled gracefully by the notifier — notifications should be queued and sent when internet access is restored.
- **External data directory (docker-compose.yml):** `setup.sh` and `.env.example` define `DATA_DIR`, but `docker-compose.yml` still uses hardcoded relative paths (`./config`, `./models`, `./data`) instead of `${DATA_DIR}/config`, etc. The external data dir works only if the repo-local directories are symlinked or if setup copies data back into the repo tree. Compose file needs updating to use `${DATA_DIR}`.

### What's Not Yet Validated / Built

- Custom-trained heron model (currently using generic COCO bird class)
- SSL/TLS for web UI
- Snapshot retention/cleanup
- Enhanced event logs with action tracking and filtering
- Generic REST webhook notification channel
- Valve actuation (ESP32 hardware not wired yet)
- Live camera feed with bounding box overlay in web UI (SSE)
- About page (project info, versions, component status)
- Admin/logs tab (view service logs from web UI)

---

## Roadmap — Current Priorities (in order)

### Priority 1: Harden Multi-Camera Detection
Multi-camera is implemented but one camera appears to be starved in practice. Fix scheduling so all enabled cameras get fair processing time.

**Acceptance criteria:**
- Both `pond-north` and `pond-south` streams processed concurrently with balanced frame rates
- Detection events include camera name for traceability
- One camera going down does not crash or starve the other
- Snapshots saved per-camera with camera name in filename
- Validated with physical camera streams (not just config)

### Priority 2: Fix Form-Based Config GUI
The structured config editor exists but has rendering and data-loading bugs.

**Acceptance criteria:**
- Enable/disable toggles for features and cameras are visually aligned and functional
- Existing cameras correctly populated from `scarguard.yml` on page load
- Add/remove cameras via the UI
- Validation before save (e.g. RTSP URL format, required fields)
- Save writes back to `scarguard.yml` (triggers hot-reload)

### Priority 3: Notification Resilience
Notifier does not handle internet outages gracefully. Notifications should queue and retry.

**Acceptance criteria:**
- On send failure (network error, timeout), notifications are queued in-memory or on disk
- Queued notifications are retried with exponential backoff when connectivity is restored
- A connectivity health check runs periodically (ping or lightweight HTTP request)
- Notification queue is bounded (configurable max size, oldest dropped if full)
- Logged clearly: queue depth, retry attempts, eventual success/failure

### Priority 4: About Page
Add an "About" page/tab to the web UI that displays project info, version, and component status at a glance.

**Acceptance criteria:**
- Accessible from the main navigation as an "About" tab/link
- Displays: project name, version (from a `VERSION` file), build info (git commit hash, build date if available)
- `VERSION` file lives in the repo root and is bumped as part of the release/tag CI workflow
- Shows component status: Redis connectivity, detector running, notifier running, camera stream status
- Shows system info: host platform, Python version, YOLO model loaded, config file path
- Lightweight — no heavy polling, snapshot of current state on page load

### Priority 5: Admin Logs Tab
Add a "Logs" tab under an admin/configuration section of the web UI to view recent service logs.

**Acceptance criteria:**
- Accessible from the web UI navigation (e.g. under an "Admin" or "System" dropdown)
- Displays recent log output from each service: detector, notifier, web
- Logs sourced from Docker container logs via the Docker socket or from log files if services write to a shared volume
- Filterable by service name and log level (info, warning, error)
- Auto-scroll / tail mode with pause option
- Reasonable log buffer (last N lines or last N minutes, configurable)

### Priority 6: SSL / TLS for Web UI
Support both HTTP and HTTPS. Generate a self-signed cert by default during setup, with the option to provide a custom cert and key.

**Acceptance criteria:**
- `setup.sh` generates a self-signed cert if none exists at the configured path
- Config in `scarguard.yml` specifies cert/key paths and whether HTTPS is enabled
- Web service listens on both HTTP (8080) and HTTPS (8443), or HTTPS-only if configured
- User can drop in their own cert/key and restart to use it

### Priority 7: Snapshot Retention & Cleanup
Snapshots accumulate on disk indefinitely. Add a configurable retention policy.

**Acceptance criteria:**
- Config field: `system.snapshot_retention_days` (default: 30)
- Background task (in web or detector service) prunes snapshots older than retention period
- Corresponding SQLite detection records are cleaned up or marked as snapshot-expired
- Runs on a schedule (daily) and at startup

### Priority 8: Detection Exclusion Zones
Suppress false positives from static objects (e.g. a heron decoy that never moves). Two tiers — implement Tier 1 now, Tier 2 is a future stretch goal.

**Tier 1 — Manual exclusion zones (implement now):**
Per-camera rectangular mask regions drawn in the web UI. Any detection whose bounding box center falls inside an exclusion zone is silently dropped (not logged, not notified). Zones are saved in `scarguard.yml` under the camera entry.

**Acceptance criteria (Tier 1):**
- Web UI overlay on camera snapshot lets user draw/resize/delete rectangular exclusion zones
- Zones stored per-camera in config: `cameras[].exclusion_zones: [{x, y, w, h, label}]`
- Detector checks every detection against zones before publishing events
- Label is optional — useful for the user to remember why a zone exists (e.g. "heron decoy")
- Zones survive config reload and service restart

**Tier 2 — Automatic static object detection (future/stretch):**
The detector tracks detections that remain in the same position across many frames over an extended period (hours/days). If a detected object hasn't moved beyond a threshold, flag it in the web UI: "This object at [camera: pond-north] has been detected 847 times in the same spot over 3 days — add an exclusion zone?" User confirms or dismisses. Don't auto-exclude without user approval.

### Priority 9: Enhanced Detection Event Logs
Improve the event log in the web UI to show richer detail and support filtering.

**Acceptance criteria:**
- Each event shows: timestamp, camera name, detected class, confidence, what actions were triggered (Discord sent, email sent, valve fired, etc.)
- Filter/sort by camera, detected class, date range
- Support for per-class or per-camera action rules in config (e.g. "bird → notify only", "heron → notify + valve", "human → log only")
- Action rules are configurable in `scarguard.yml` and the GUI config editor

### Priority 10: Live Camera Feed in Web UI
SSE or WebSocket endpoint that streams annotated frames (with bounding boxes on detections) to the dashboard.

**Acceptance criteria:**
- At least one camera feed visible in the web UI dashboard
- Bounding boxes drawn on detected objects in real-time
- Feed degrades gracefully if stream drops (shows "offline" state, auto-reconnects)

### Priority 11: Custom REST API Notification Channel
Add a generic outbound webhook/REST API notification type. On detection events, POST a JSON payload to a user-configured URL. This is the integration point for valve actuation (ESP32 listening on a REST endpoint), home automation, or any external system.

**Acceptance criteria:**
- New notification channel type `webhook` in config alongside discord and email
- Configurable: URL, HTTP method (POST/PUT), custom headers, optional auth token
- Payload includes: event timestamp, camera, detected class, confidence, snapshot URL
- Retry with backoff on failure (same pattern as other notification channels)
- Multiple webhook endpoints supported (e.g. one for valves, one for Home Assistant)

### Priority 12: Valve Actuation
ESP32 + ESPHome controlling 4x Orbit DC solenoid valves. Can be triggered via the REST webhook (Priority 11) or MQTT — owner's choice. Randomized spray patterns.

**Acceptance criteria:**
- Valve controller accepts commands via REST endpoint (and optionally MQTT)
- Valve selection, duration, and delay are randomized within configured bounds
- Independent cooldown prevents over-watering
- Config section in `scarguard.yml` controls all valve parameters

### Priority 13: Custom Heron Model Training
Replace the generic COCO bird model with a fine-tuned model that distinguishes heron species. This is a data collection and training task, not primarily a code task.

---

## Completed Work

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Detection engine (RTSP + YOLO + SQLite + Redis) | ✅ Complete |
| 2 | Notifications (Discord webhook + Email SMTP) | ✅ Complete & Validated |
| 3 | Web UI (dashboard, events, config editor, model upload) | ✅ Complete |
| — | CI/CD pipeline (GitHub Actions, GHCR, self-hosted runners) | ✅ Complete |
| — | Docker Compose orchestration + setup.sh installer | ✅ Complete |
| — | External data directory (config/data/models outside repo) | ✅ Complete |
| — | Config hot-reload (detector + notifier restart on config change) | ✅ Complete |
| — | Form-based config GUI (initial implementation) | ⚠️ Buggy — see Priority 2 |
| — | Multi-camera detection (initial implementation) | ⚠️ Needs hardening — see Priority 1 |

---

## Architecture

### Hardware
- **Compute:** NVIDIA Jetson Orin Nano, JetPack 6.2.1 (L4T 36.4.7), hardwired into UniFi fabric
- **Cameras:** 2x UniFi cameras (G3 Flex + G5 Flex) streaming RTSP via UniFi Protect on a UDM
- **Valves (future):** 4x Orbit DC solenoid valves (~6VDC, from Yard Enforcer), controlled by ESP32 + MOSFETs
- **Network:** All devices on same LAN behind UDM. Internal domain: `int.sentania.net`

### Software Stack — Docker Compose Services

```
scarguard/
├── docker-compose.yml              # Application services
├── setup.sh                        # First-run setup script
├── pyproject.toml                  # Python project config (linting, testing)
├── .env.example                    # Environment variable template
├── config/
│   └── scarguard.example.yml      # Example config template (copied to DATA_DIR by setup.sh)
├── models/                          # YOLO model files (.pt, .engine)
├── data/
│   └── scarguard.db                # SQLite database
├── services/
│   ├── detector/                    # RTSP ingestion + YOLO inference
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── src/
│   │       ├── main.py             # Entry point: pulls RTSP frames, runs inference
│   │       ├── stream.py           # RTSP stream management (OpenCV VideoCapture)
│   │       ├── detector.py         # YOLO model loading/inference wrapper
│   │       ├── events.py           # Detection event processing, dedup, cooldown
│   │       └── publisher.py        # Publishes detection events to Redis
│   ├── web/                         # FastAPI + Jinja web UI
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   ├── src/
│   │   │   ├── main.py             # FastAPI app entry point
│   │   │   ├── routes/
│   │   │   │   ├── dashboard.py    # Live view, status, arm/disarm
│   │   │   │   ├── events.py       # Detection event log
│   │   │   │   ├── config.py       # Config viewer/editor
│   │   │   │   ├── models.py       # Model upload/swap
│   │   │   │   ├── about.py        # About page (version, component status)
│   │   │   │   └── admin.py        # Admin tools (service logs viewer)
│   │   │   ├── api/
│   │   │   │   └── v1.py           # REST API for programmatic access
│   │   │   └── db.py               # SQLite access layer
│   │   ├── templates/               # Jinja2 HTML templates
│   │   └── static/                  # CSS, JS
│   └── notifier/                    # Notification dispatch service
│       ├── Dockerfile
│       ├── requirements.txt
│       └── src/
│           ├── main.py             # Listens for detection events, dispatches
│           ├── discord.py          # Discord webhook sender
│           └── email.py            # Email sender (SMTP)
├── shared/
│   └── models.py                    # Shared Pydantic data models for events
├── infra/
│   ├── orin-runner/
│   │   ├── Dockerfile              # ARM64 GitHub Actions runner for Orin
│   │   └── entrypoint.sh           # Runner entrypoint script
│   ├── docker-compose.runner.yml   # Compose file for Orin runner
│   └── orin-setup.sh               # Host setup script (Docker + NVIDIA runtime)
└── .github/
    └── workflows/
        └── ci.yml                   # Lint, test, build container images
```

### Service Communication
- **Between services:** Redis pub/sub as internal message bus. The detector publishes detection events; notifier and web UI subscribe.
- **Config:** All services read from mounted `config/scarguard.yml` in the external data directory. The web UI can write to it. Detector and notifier auto-restart on config file changes (hot-reload).
- **Database:** SQLite at `data/scarguard.db`, shared volume between web and detector.

### Container Base Images
- **detector:** `dustynv/l4t-pytorch:r36.4.0` (provides CUDA, cuDNN, PyTorch, TensorRT). Compatible with L4T r36.4.7. GPU accessed via NVIDIA Container Runtime (`runtime: nvidia` in Compose).
- **web and notifier:** `python:3.11-slim` — no GPU needed.

### Container Registry
All application images are pushed to **GitHub Container Registry (ghcr.io)**. The Orin pulls from GHCR for production runs.

---

## Design Decisions — Do Not Change Without Discussion

1. **Docker Compose is the deployment target.** No Kubernetes, no Swarm. `docker compose up` is the full deployment.
2. **Single config file** (`scarguard.yml`) is the source of truth for all runtime config. Don't fragment into per-service configs. After Priority 2a, this file lives in the external data directory, not in the repo.
3. **Redis pub/sub** is the inter-service bus. Don't add Kafka, RabbitMQ, or anything heavier.
4. **SQLite** is the database. Don't switch to Postgres — this runs on a single Jetson with one writer.
5. **Python 3.11** across all services. Don't upgrade without testing against the L4T base image.
6. **Notifications are working — don't refactor them.** Both email and Discord webhook dispatch are validated. New notification types (REST webhooks) are additive — don't break existing channels.
7. **RTSP streams will drop.** The detector must reconnect gracefully with exponential backoff. Never crash on a dropped stream.
8. **Snapshots are files on disk**, served by the web service from `data/snapshots/`. Don't move them to a blob store or database.
9. **Prefer REST over MQTT for external integrations.** Valve actuation and home automation should use outbound REST webhooks unless there's a specific reason to add MQTT. Keep the dependency surface small.
10. **Data directory is external to the repo.** `git pull` never touches user config, models, database, or snapshots. The repo is code-only.

---

## Config File Format (scarguard.yml)

```yaml
system:
  armed: true
  log_level: info
  snapshot_retention_days: 30    # Prune snapshots older than this

web:
  http_port: 8080
  https_port: 8443
  ssl:
    enabled: false               # Set true to enable HTTPS
    cert_path: /config/certs/scarguard.crt   # Self-signed generated by setup.sh, or provide your own
    key_path: /config/certs/scarguard.key

cameras:
  - name: pond-north
    rtsp_url: "rtsp://172.16.0.1:7447/STREAM_TOKEN_1"
    enabled: true
    resolution: 720
    exclusion_zones:              # Regions where detections are ignored
      - x: 320
        y: 180
        w: 80
        h: 120
        label: "heron decoy"
  - name: pond-south
    rtsp_url: "rtsp://172.16.0.1:7447/STREAM_TOKEN_2"
    enabled: true
    resolution: 720
    exclusion_zones: []

detection:
  model_path: /models/best.engine
  confidence_threshold: 0.25
  target_classes:
    - great_blue_heron
    - green_heron
    - duck
    - raccoon
  cooldown_seconds: 30
  frame_skip: 2

# Per-class (or per-camera) action rules — what happens on detection
# If no rule matches, default is to send all enabled notifications.
action_rules:
  - match:
      class: great_blue_heron
    actions: [discord, email, webhook]    # Full alert
  - match:
      class: bird                          # Generic bird (COCO class)
    actions: [discord]                     # Notify only, no valve
  - match:
      class: human
    actions: [log]                         # Log only, no notification
  - match:
      camera: pond-south
      class: raccoon
    actions: [discord, webhook]

notifications:
  discord:
    enabled: true
    webhook_url: "https://discord.com/api/webhooks/..."
    mention_role: ""
    include_snapshot: true
  email:
    enabled: false
    smtp_host: ""
    smtp_port: 587
    smtp_user: ""
    smtp_pass: ""
    to_addresses: []
    include_snapshot: true
  webhooks:                                # Generic REST API notification channels
    - name: valve-controller
      enabled: false
      url: "http://192.168.1.x/api/fire"
      method: POST
      headers:
        Authorization: "Bearer YOUR_TOKEN"
      include_snapshot_url: true            # Include snapshot URL in payload (not the image itself)
    - name: home-assistant
      enabled: false
      url: "http://homeassistant.local:8123/api/webhook/scarguard"
      method: POST

# Future: valve actuation config (may be replaced by webhook + ESP32 REST endpoint)
# valves:
#   spray_duration_min_sec: 3
#   spray_duration_max_sec: 8
#   valve_count: 4
#   randomize: true
#   cooldown_seconds: 60

redis:
  host: redis
  port: 6379
```

---

## Detection Logic

1. Pull frames from each RTSP stream (OpenCV `VideoCapture`)
2. Run YOLO inference on GPU (`model.predict()`)
3. Filter results by target classes and confidence threshold
4. Apply cooldown dedup (don't fire 10 events for same heron standing there)
5. On new detection event:
   - Save to SQLite (timestamp, class, confidence, camera, snapshot path)
   - Publish to Redis pub/sub channel `scarguard:detections`
   - Save annotated snapshot frame to disk
6. Notifier picks up events from Redis and dispatches to configured channels
7. Web UI subscribes to Redis for live event feed via SSE

---

## CI/CD Strategy

### Runners
- **x86 runners (existing org runners):** Lint, type checking, pytest for web + notifier, build and push non-GPU images to GHCR
- **Orin runner (self-hosted, containerized):** Build detector image (ARM64 + L4T base), GPU integration tests, full Compose smoke tests

### x86 Runner Details
- Containerized GitHub Actions runner on an ubuntu24 host, Dockerfile managed out of band
- Docker socket mount from host — runner container issues Docker commands against host daemon
- Labels: `self-hosted`, `linux`, `X64`, `docker`

### Orin Runner Details
- Containerized GitHub Actions runner on the Orin (ARM64 Dockerfile in `infra/orin-runner/`)
- Docker socket mount from host — runner container issues Docker commands against host daemon
- GPU accessible because builds/tests run on host Docker (not nested)
- Labels: `self-hosted`, `linux`, `arm64`, `jetson`

### Build & Deploy Flow
```
Push to main
  ├── x86 runners:
  │   ├── Lint + type check (all services)
  │   ├── pytest (web, notifier — no GPU needed)
  │   └── Build + push web/notifier images to ghcr.io
  │
  └── Orin runner:
      ├── Build detector image locally (ARM64 + L4T base)
      ├── Run GPU smoke test (load model, single frame inference)
      └── Push detector image to ghcr.io
```

### Runner Image Updates
Currently manual: SSH into Orin, rebuild runner image, restart container.

---

## Development Guidelines

- **Python 3.11** across all services
- **Type hints** on all functions
- **Pydantic models** for all data structures (events, config)
- **Logging:** Python `logging` module, structured JSON output, respect `log_level` from config
- **Error handling:** RTSP streams will drop — detector must reconnect gracefully with backoff
- **Testing:** pytest. Focus on detection logic and event pipeline. Don't over-test for MVP.
- **No over-engineering:** This is a pond guardian, not a distributed platform. Keep it simple.

---

## RTSP Notes

- UniFi Protect RTSP must be enabled per-camera in the Protect UI on the UDM
- RTSP URL format: `rtsp://172.16.0.1:7447/<stream_token>`
- Use 720p substream for inference — 4K wastes GPU cycles
- OpenCV `VideoCapture` handles RTSP natively; set `cv2.CAP_PROP_BUFFERSIZE` to 1 to reduce frame lag
- Camera models: G3 Flex and G5 Flex (G3 may be replaced with another G5)

---

## Host Prerequisites

Before running any containers, the Orin host needs:

1. Docker Engine installed
2. NVIDIA Container Toolkit installed and configured
3. `nvidia-container-runtime` set as default runtime
4. GitHub Actions runner container running (see `infra/` directory)

Use `infra/orin-setup.sh` for steps 1-3. Test with: `docker run --rm --runtime=nvidia --gpus all dustynv/l4t-pytorch:r36.4.0 python3 -c "import torch; print(torch.cuda.is_available())"`

---

## Future Ideas (Unprioritized)
- SMS/iMessage notifications
- Automated Orin runner updates via SSH from x86 runners
- Second Orin or AGX as dedicated build runner
- Multi-model support (seasonal species profiles)
- Scheduled arm/disarm (arm at dawn, disarm at dusk)