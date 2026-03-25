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

## Known Issues / Buggy

## Not Yet Built

- Custom-trained heron model (currently using generic COCO bird class)
- Valve actuation (ESP32 hardware not wired yet)
- App security / user accounts (admin logs endpoint exposes Docker socket — must be gated before external exposure)

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
