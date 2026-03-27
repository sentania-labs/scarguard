# ScarGuard — Current Status

## What's Working (Validated)

- **Detection pipeline:** Detector service loads YOLO model, pulls RTSP frames, runs inference, logs to SQLite, publishes to Redis. Running with basic COCO `bird` class model.
- **Email notifications:** SMTP dispatch with snapshot attachment — tested and confirmed.
- **Discord notifications:** Webhook dispatch with snapshot image — tested and confirmed.
- **Webhook notifications:** Generic HTTP/HTTPS webhook channel (POST or PUT, optional Bearer auth).
- **Named notification channels:** Multi-instance per type (`notifications.channels`), each with a unique name. Legacy flat `discord`/`email` keys still work.
- **Web UI:** Dashboard, event log, config editor (form + raw YAML), model upload — functional.
- **CI/CD:** GitHub Actions workflow builds and pushes images to GHCR. x86 and Orin self-hosted runners operational.
- **Docker Compose stack:** All four services (redis, detector, web, notifier) start and communicate correctly.
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

## Known Issues / Buggy

None currently identified.

## Not Yet Built

- Custom-trained heron model (have the tooling now, need labeled data)
- Physical deterrence — planned as companion project "Scar's Revenge" (ESP32 valve controller receiving ScarGuard webhooks)

See [ROADMAP.md](ROADMAP.md) for upcoming features (17–26) and [ROADMAP_ARCHIVE.md](ROADMAP_ARCHIVE.md) for completed feature history.

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
