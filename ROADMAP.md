# ScarGuard — Roadmap

Current priorities in order. Each item includes acceptance criteria.

---

## Priority 2: Notification Resilience

Notifier does not handle internet outages gracefully. Notifications should queue and retry.

**Acceptance criteria:**
- On send failure (network error, timeout), notifications are queued in-memory or on disk
- Queued notifications are retried with exponential backoff when connectivity is restored
- A connectivity health check runs periodically (ping or lightweight HTTP request)
- Notification queue is bounded (configurable max size, oldest dropped if full)
- Logged clearly: queue depth, retry attempts, eventual success/failure

---

## Priority 3: Admin Logs Tab

Add a "Logs" tab under admin/configuration section of the web UI.

**Acceptance criteria:**
- Accessible from web UI navigation (e.g. under "Admin" or "System" dropdown)
- Displays recent log output from each service: detector, notifier, web
- Logs sourced from Docker container logs via Docker socket or from shared volume log files
- Filterable by service name and log level (info, warning, error)
- Auto-scroll / tail mode with pause option
- Reasonable log buffer (last N lines or last N minutes, configurable)

---

## Priority 4: SSL / TLS for Web UI

Support both HTTP and HTTPS. Self-signed cert by default, option for custom cert.

**Acceptance criteria:**
- `setup.sh` generates a self-signed cert if none exists at the configured path
- Config in `scarguard.yml` specifies cert/key paths and whether HTTPS is enabled
- Web service listens on both HTTP (8080) and HTTPS (8443), or HTTPS-only if configured
- User can drop in their own cert/key and restart to use it

---

## Priority 5: Snapshot Retention & Cleanup

Snapshots accumulate indefinitely. Add configurable retention policy.

**Acceptance criteria:**
- Config field: `system.snapshot_retention_days` (default: 30)
- Background task prunes snapshots older than retention period
- Corresponding SQLite records cleaned up or marked as snapshot-expired
- Runs on schedule (daily) and at startup

---

## Priority 6: Detection Exclusion Zones

Suppress false positives from static objects (e.g. heron decoy). Two tiers.

**Tier 1 — Manual exclusion zones (implement now):**
Per-camera rectangular mask regions drawn in the web UI. Detection whose bounding box center falls inside an exclusion zone is silently dropped. Zones saved in `scarguard.yml` under the camera entry.

**Acceptance criteria (Tier 1):**
- Web UI overlay on camera snapshot lets user draw/resize/delete rectangular exclusion zones
- Zones stored per-camera: `cameras[].exclusion_zones: [{x, y, w, h, label}]`
- Detector checks every detection against zones before publishing events
- Label is optional (e.g. "heron decoy")
- Zones survive config reload and service restart

**Tier 2 — Automatic static object detection (future/stretch):**
Track detections that remain in the same position across many frames over hours/days. If an object hasn't moved beyond threshold, prompt user in web UI to add exclusion zone. Never auto-exclude without user approval.

---

## Priority 7: Enhanced Detection Event Logs

Richer detail and filtering in the web UI event log.

**Acceptance criteria:**
- Each event shows: timestamp, camera name, detected class, confidence, actions triggered
- Filter/sort by camera, detected class, date range
- Per-class or per-camera action rules in config (e.g. "bird → notify only", "heron → notify + valve")
- Action rules configurable in `scarguard.yml` and GUI config editor

---

## Priority 8: Live Camera Feed in Web UI

SSE or WebSocket endpoint streaming annotated frames with bounding boxes.

**Acceptance criteria:**
- At least one camera feed visible in dashboard
- Bounding boxes drawn on detected objects in real-time
- Graceful degradation on stream drop (shows "offline", auto-reconnects)

---

## Priority 9: Custom REST API Notification Channel

Generic outbound webhook/REST notification type for external integrations.

**Acceptance criteria:**
- New `webhook` notification channel type alongside discord and email
- Configurable: URL, HTTP method (POST/PUT), custom headers, optional auth token
- Payload includes: event timestamp, camera, detected class, confidence, snapshot URL
- Retry with backoff on failure
- Multiple webhook endpoints supported

---

## Priority 10: Custom Heron Model Training

Replace generic COCO bird model with fine-tuned model distinguishing heron species. Data collection and training task, not primarily code.

---

## Future Ideas (Unprioritized)

- SMS/iMessage notifications
- Automated Orin runner updates via SSH from x86 runners
- Second Orin or AGX as dedicated build runner
- Multi-model support (seasonal species profiles)
- Scheduled arm/disarm (arm at dawn, disarm at dusk)
- App security, user accounts
- Different detection models and classes per stream