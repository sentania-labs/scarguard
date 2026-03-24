# ScarGuard — Current Status

## What's Working (Validated)

- **Detection pipeline:** Detector service loads YOLO model, pulls RTSP frames, runs inference, logs to SQLite, publishes to Redis. Running with basic COCO `bird` class model.
- **Email notifications:** SMTP dispatch with snapshot attachment — tested and confirmed.
- **Discord notifications:** Webhook dispatch with snapshot image — tested and confirmed.
- **Web UI:** Dashboard, event log, config editor, model upload — functional.
- **CI/CD:** GitHub Actions workflow builds and pushes images to GHCR. x86 and Orin self-hosted runners operational.
- **Docker Compose stack:** All four services (redis, detector, web, notifier) start and communicate correctly.
- **Config hot-reload:** Detector and notifier poll config and apply changes in-process (no service restart required). Detector dynamically updates armed flag, threshold/classes, cooldown, frame_skip, and enabled cameras; notifier dynamically rebuilds its notifier set. Web service reads config on request, so most config display changes do not require restart.
- **External data directory:** Application assets (config, data, models, snapshots) stored externally to the project repo.
- **Notifier resilience:** Internet interruptions now handled gracefully

## Known Issues / Buggy

## Not Yet Built

- Custom-trained heron model (currently using generic COCO bird class)
- Enhanced event logs with action tracking and filtering
- Generic REST webhook notification channel
- Valve actuation (ESP32 hardware not wired yet)
- Live camera feed with bounding box overlay in web UI (SSE)
- Detection exclusion zones
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
