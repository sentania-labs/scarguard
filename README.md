# ScarGuard

An AI-powered wildlife detection and notification system. ScarGuard watches RTSP camera feeds for target species — primarily great blue herons — and sends real-time notifications so you (or downstream automation) can respond to protect a backyard koi pond.

The reference deployment runs on an NVIDIA Jetson Orin Nano with UniFi cameras, but ScarGuard works with any RTSP-capable camera and any system that can run Docker with an NVIDIA GPU.

Named after Scar (aka Kroger), a koi who survived a heron attack and lived to tell the tale.

---

## The Problem

Great blue herons are patient, methodical hunters. A single bird can empty a koi pond in a morning. Traditional deterrents — plastic owls, reflective tape — lose their effectiveness quickly as the birds habituate to them. What works is unpredictability: a response that varies in timing and pattern, triggered only when a bird is actually present.

ScarGuard watches the pond around the clock, identifies threats with a YOLO vision model, sends targeted notifications (Discord, email, webhooks, ntfy), and triggers physical deterrent devices (sprinklers, lights, sirens) via the Tuya Cloud API. The detect → notify → deter loop runs end-to-end with no external dependencies.

---

## Goals

- **Accurate, low-latency detection** — identify herons, ducks, and raccoons from live camera feeds with enough confidence to act, fast enough to matter
- **Flexible notification routing** — route detections to the right channels (Discord, email, webhooks) based on species, camera, and time of day; downstream systems decide what action to take
- **Minimal false positives** — don't fire notifications every time a leaf blows past; confidence thresholds, cooldown windows, and exclusion zones keep the system from crying wolf
- **Always-on, self-healing** — RTSP streams drop; cameras reboot; the system must reconnect gracefully and resume without human intervention
- **Observable** — a web UI shows live status, recent detections with annotated snapshots, and configuration; Discord, email, and webhook notifications keep the owner in the loop
- **Maintainable** — the whole stack runs in Docker Compose; deploying a new model or changing config requires no SSH access

---

## Capabilities

### Detection
- Real-time inference on live RTSP streams using a YOLO model on GPU (TensorRT-optimized on Jetson; CUDA on x86)
- Multi-camera support — each camera runs in its own thread with independent cooldown tracking
- Target species configurable via `detection.target_classes` (extensible via model swap)
- Per-camera exclusion zones to suppress detections from static false-positive sources
- Action rules for per-class, per-camera routing to specific notification channels
- Configurable confidence threshold, cooldown window, and frame skip
- Clean snapshots saved per detection with bounding box coordinates stored separately (rendered in browser)

### Notifications
- **Discord** — webhook messages with optional snapshot attachment and role mention
- **Email** — SMTP with optional snapshot attachment, multiple recipients
- **Webhooks** — generic HTTP/HTTPS POST or GET to any URL, with custom headers
- Named, multi-instance channels — run two Discord webhooks, two email addresses, multiple webhooks side-by-side
- Per-camera notification rules — route heron detections on pond-south to the heron alert channel, raccoon detections on back-yard to email only, etc. (v0.13.3: renamed from `action_rules`)
- Retry with exponential backoff on transient failures

### Web UI
- **Dashboard** — arm/disarm toggle, latest detection, today's count, schedule status
- **Events** — paginated detection log with filters (camera, class, date range), snapshot overlays with bounding box rendering, real-time inserts via SSE, per-event feedback
- **Live Feed** — SSE-driven annotated detection snapshots with offline indicator and auto-reconnect
- **Settings** — sub-tabbed config editor (System / Detection / Cameras / Notifications / Advanced), plus raw YAML view. Per-camera model, confidence, classes, exclusion zones, notification rules, and deterrent rules.
- **System Stats** — real-time CPU, RAM, GPU usage and temperature, per-camera inference FPS, rolling charts
- **Logs** — live service log tail with level filtering and pause/resume
- **Training Data** — per-class feedback breakdown, dataset quality warnings, YOLO export
- **Model Evaluation** — side-by-side model comparison on labeled snapshots, SSE progress, promotion button
- **Models** — upload, list, and manage YOLO model files
- **Users** — add/disable/delete users, change passwords, manage API tokens
- **About** — version, build date, component health, active model

### Operations
- Docker Compose stack — `docker compose up` is the full deployment
- All config in a single `scarguard.yml` — hot-reloaded by all services without restart
- CI/CD via GitHub Actions — x86 runners for web/notifier; self-hosted Orin runner for ARM64 detector
- Images published to GitHub Container Registry (ghcr.io)
- HTTPS support — Caddy reverse proxy with automatic Let's Encrypt, manual certs, or HTTP-only mode
- Session-based authentication — bcrypt passwords, configurable session timeout, login lockout
- Scheduled arm/disarm — fixed time or solar (sunrise/sunset via `astral`)
- Snapshot retention policy — configurable retention days, daily pruning

---

## Hardware

ScarGuard works with any RTSP-capable cameras and any Docker host with an NVIDIA GPU. The table below is the **reference setup** — not a requirements list.

| Component | Reference Setup | Minimum Requirement |
|-----------|----------------|---------------------|
| Compute | NVIDIA Jetson Orin Nano, JetPack 6.2.1 | Any Docker host with NVIDIA GPU (Jetson or x86 CUDA) |
| Cameras | 2x UniFi (G3 Flex + G5 Flex) via UniFi Protect RTSP | Any camera with RTSP output |
| Deterrence | Tuya/Smart Life WiFi devices (sprinkler valves, lights, sirens) controlled via Cloud API | Any Tuya-compatible smart device |
| Network | Any network with layer 2 connectivity between host and cameras | Host must reach camera RTSP streams directly |

> **Platform support:** Two detector images are available: `scarguard-detector` for Jetson (ARM64/L4T) and `scarguard-detector-x86` for x86 systems. The x86 image uses CUDA when an NVIDIA GPU is present and falls back to CPU inference when it's not. `setup.sh` auto-detects your platform. See [BENCHMARKS.md](BENCHMARKS.md) for inference performance by platform.

---

## Stack

| Service | Role | Base Image |
|---------|------|-----------|
| `detector` | RTSP ingestion, YOLO inference, event publishing | Jetson: `dustynv/l4t-pytorch:r36.4.0`; x86: `pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime` |
| `web` | FastAPI + Jinja UI, REST API, SQLite access | `python:3.11-slim` |
| `notifier` | Redis subscriber, Discord + email + webhook + ntfy dispatch | `python:3.11-slim` |
| `deterrent` | Tuya Cloud device control (sprinklers, lights, sirens) | `python:3.11-slim` |
| `log-streamer` | Tails container logs, publishes to Redis for web UI | `python:3.11-slim` |
| `caddy` | HTTPS reverse proxy | `caddy:2-alpine` |
| `redis` | Internal message bus | `redis:alpine` |

Services communicate over Redis pub/sub. All configuration lives in a single `scarguard.yml` file mounted into each container.

---

## Deploy

> **You don't need to build anything.** Pre-built images are published to GitHub Container Registry automatically. Just clone, configure, and run.

The instructions below target the reference setup (Jetson Orin Nano + UniFi cameras). Adapt as needed for your hardware.

### Prerequisites

- **Jetson:** NVIDIA Jetson (Orin Nano or similar) running JetPack 6.x with NVIDIA Container Runtime
- **x86 with GPU:** Any Linux x86_64 host with Docker and NVIDIA Container Runtime (`nvidia-ctk` / `nvidia-container-toolkit`)
- **x86 CPU-only:** Any Linux x86_64 host with Docker (slower inference, no GPU required)
- Internet connection (to pull images from ghcr.io)
- One or more cameras with RTSP streaming enabled

> **Model files:** YOLO `.pt` model files work cross-platform. TensorRT `.engine` files are architecture-specific and must be regenerated per device (ultralytics does this automatically on first load).

### 1. Clone the repo

```bash
git clone https://github.com/sentania-labs/scarguard.git
cd scarguard
```

### 2. Run the setup script

```bash
bash setup.sh
```

The script will:
- Detect your platform (Jetson or x86) and select the correct detector image
- Check that Docker is installed; check for NVIDIA container runtime (required on Jetson, optional on x86)
- Ask which HTTP port to use (default: 80)
- Create `config/scarguard.yml` from the example template (stored in the `scarguard-config` named volume)
- Ask how you'll access ScarGuard (LAN only / internet with Let's Encrypt / own certificates)
- Offer to download a starter YOLO model (detects generic birds — good for testing the pipeline)
- Pull all pre-built images from GHCR
- Prompt you to create the initial admin account

### 3. Edit your configuration

Use the web UI config editor after starting the stack, or edit directly via a temporary container:

```bash
docker run --rm -it -v scarguard-config:/config alpine vi /config/scarguard.yml
```

At minimum, set your camera RTSP URLs:

```yaml
cameras:
  - name: pond-north
    rtsp_url: "rtsp://YOUR_CAMERA_IP:7447/YOUR_STREAM_TOKEN"
    enabled: true
```

For a more complete initial setup, see the examples below or jump to [Feature Guides](#feature-guides).

**Cameras with exclusion zones** (suppress a static decoy or blind spot):

```yaml
cameras:
  - name: pond-north
    rtsp_url: "rtsp://YOUR_CAMERA_IP:7447/YOUR_STREAM_TOKEN"
    enabled: true
    exclusion_zones:
      - x: 0.72   # normalized 0–1 from left
        y: 0.10   # normalized 0–1 from top
        w: 0.12
        h: 0.18
        label: "heron decoy"
```

**Named notification channels** (Discord + email + custom webhook):

```yaml
notifications:
  channels:
    - name: pond-discord
      type: discord
      enabled: true
      webhook_url: "https://discord.com/api/webhooks/..."
      include_snapshot: true

    - name: owner-email
      type: email
      enabled: true
      smtp_host: smtp.gmail.com
      smtp_port: 587
      smtp_user: you@gmail.com
      smtp_pass: "your-app-password"
      to_addresses: [you@gmail.com]
      include_snapshot: true

    - name: deterrent-webhook       # downstream system (e.g. Scar's Revenge)
      type: webhook
      enabled: true
      url: "http://192.168.1.50/api/spray"
      method: POST
      headers: { "Authorization": "Bearer my-token" }
```

**Arm/disarm schedule** (solar-based, adjust to your coordinates):

```yaml
system:
  schedule:
    enabled: true
    use_solar: true
    latitude: 39.9526
    longitude: -75.1652
```

### 4. Add a YOLO model (if you skipped the starter download)

Place your `.pt` or `.engine` file in `models/`, then update `config/scarguard.yml`:

```yaml
detection:
  model_path: /models/your-model.pt
  target_classes:
    - great_blue_heron
    - green_heron
```

> The starter model (`yolov8n.pt`) uses the COCO `bird` class and is useful for verifying the system is working before you have a custom-trained model. It will not distinguish herons from sparrows.

### 5. Start ScarGuard

```bash
docker compose up -d
```

### 6. Open the web UI

```
http://<your-orin-ip>
```

You'll be redirected to the login page. Use the admin account created during setup.

> **Note:** The Caddy reverse proxy listens on port 80 (HTTP) by default.
> To enable HTTPS, go to Settings > TLS in the web UI and choose Automatic
> (Let's Encrypt) or Manual (your own certificates).

---

### Useful Commands

```bash
# View live logs from all services
docker compose logs -f

# View logs from a specific service
docker compose logs -f detector

# Stop all services
docker compose down

# Update to the latest images
docker compose pull && docker compose up -d

# Pin to a specific release
echo "IMAGE_TAG=v1.0.0" >> .env
docker compose pull && docker compose up -d
```

---

### Enabling HTTPS

ScarGuard uses a Caddy reverse proxy for TLS termination. Three modes are available, configurable via the web UI (Settings > TLS) or `scarguard.yml`:

#### Mode 1 — Off (default)

HTTP only. No TLS configuration needed. This is the right choice for LAN-only access.

#### Mode 2 — Automatic (Let's Encrypt)

Caddy obtains and renews a certificate from Let's Encrypt automatically. Requires a public domain name pointing to your server's IP.

**Web UI:** Settings > TLS > Mode: Automatic > enter your domain > Save.

**Or edit `scarguard.yml`:**

```yaml
tls:
  mode: auto
  domain: scarguard.example.com
```

Caddy picks up the change within a few seconds. Port 443 must be reachable from the internet for the ACME challenge.

#### Mode 3 — Manual (your own certificates)

Use certificates from an internal CA or another provider. Place `cert.pem` and `key.pem` in the config volume's `certs/` directory:

```bash
docker run --rm -v scarguard-config:/config alpine mkdir -p /config/certs
# Copy your cert and key into the volume:
docker run --rm \
    -v scarguard-config:/config \
    -v /path/to/cert.pem:/src/cert.pem:ro \
    -v /path/to/key.pem:/src/key.pem:ro \
    alpine sh -c 'cp /src/cert.pem /config/certs/cert.pem && cp /src/key.pem /config/certs/key.pem && chmod 600 /config/certs/key.pem'
```

Then set TLS mode to Manual in the web UI, or in `scarguard.yml`:

```yaml
tls:
  mode: manual
  cert_path: /config/certs/cert.pem
  key_path: /config/certs/key.pem
```

#### Changing the external ports

The default ports are 80 (HTTP) and 443 (HTTPS). To use different ports, edit `.env`:

```bash
HTTP_PORT=8080
HTTPS_PORT=8443
```

Then restart: `docker compose up -d caddy`

---

### Upgrading from a previous version

```bash
git pull
docker compose pull
docker compose up -d
```

That's all that's needed. HTTP continues to work on your existing port with no config changes.

---

## Feature Guides

### Notifications & Named Channels

ScarGuard sends alerts through named notification channels. Each channel has a unique name, a type (discord, email, or webhook), and its own settings. You can define multiple instances of the same type — for example, one Discord webhook for the pond camera and a separate one for a home-automation channel.

All channels are defined under `notifications.channels` in `scarguard.yml`:

```yaml
notifications:
  channels:
    # Discord webhook — pond alerts channel
    - name: pond-discord
      type: discord
      enabled: true
      webhook_url: "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN"
      mention_role: "123456789"   # optional: Discord role ID to ping
      include_snapshot: true      # attach the detection snapshot image

    # Email via SMTP
    - name: owner-email
      type: email
      enabled: true
      smtp_host: smtp.gmail.com
      smtp_port: 587
      smtp_user: you@gmail.com
      smtp_pass: "your-app-password"
      to_addresses:
        - you@gmail.com
        - partner@gmail.com
      include_snapshot: true

    # Generic HTTP webhook — notify a downstream system (e.g. Scar's Revenge deterrent controller)
    - name: deterrent-webhook
      type: webhook
      enabled: true
      url: "http://192.168.1.50/api/spray"
      method: POST
      headers:
        Authorization: "Bearer my-device-token"
      include_snapshot_url: true  # include snapshot URL in the POST body

    # Home Assistant webhook
    - name: home-assistant
      type: webhook
      enabled: true
      url: "http://homeassistant.local:8123/api/webhook/scarguard-alert"
      method: POST
```

All channels can be added, edited, and enabled/disabled from the **Settings → Notification Channels** section of the web UI without editing the YAML directly.

> **Legacy keys removed (v0.13.2):** The flat `notifications.discord` and `notifications.email` blocks are no longer read by the notifier, and `config_store.save()` strips them on the next save. Named channels under `notifications.channels` are now the only supported format.

---

### Per-Camera Rules — Notifications and Deterrents

Two parallel rule systems live on each camera.  Both are lists of `{class_name,
<target>}` entries evaluated top-down — the first rule whose `class_name` matches
wins (`*` is the wildcard).

- **`notification_rules`** — routes detections to specific named channels.
  Without any rules defined, every detection notifies every enabled channel.
  *(v0.13.3 renamed this from `action_rules`; old configs are auto-migrated on
  load.)*
- **`deterrent_rules`** (v0.13.3+) — fires named deterrent groups.  Empty means
  *no deterrent action* — deterrents are explicit opt-in.

Example camera block:

```yaml
cameras:
  - name: pond-south
    rtsp_url: rtsps://...
    notification_rules:
      - class_name: great_blue_heron
        channels: [pond-discord, owner-email]
      - class_name: "*"
        channels: [pond-discord]
    deterrent_rules:
      - class_name: great_blue_heron
        groups: [thermonuclear]        # all deterrents
      - class_name: raccoon
        groups: [minor]                # siren only
      # no wildcard → anything else on this camera fires no deterrent
```

Both rule tables are edited in the web UI on the **Config → Cameras** sub-tab.
Deterrent groups themselves live on **/admin/deterrent → Groups**.

---

### Detection Exclusion Zones

Exclusion zones suppress detections from specific areas of a camera frame — useful for ignoring a heron decoy statue, a bush that blows in the wind, or any other persistent false-positive source.

**Drawing zones in the web UI:**
1. Open **Settings → Cameras**
2. Find the camera and click **Edit Exclusion Zones**
3. A snapshot from the camera is displayed with an overlay canvas
4. Click and drag to draw a rectangular zone
5. Optionally add a label (e.g. "heron decoy") for reference
6. Click **Save** — the zones are written to `scarguard.yml` and hot-reloaded into the detector

Zones are stored as normalized coordinates (0–1) relative to frame size, so they remain accurate even if resolution changes:

```yaml
cameras:
  - name: pond-north
    rtsp_url: "rtsp://..."
    exclusion_zones:
      - x: 0.72    # left edge of zone (fraction of frame width)
        y: 0.10    # top edge of zone (fraction of frame height)
        w: 0.12    # zone width
        h: 0.18    # zone height
        label: "heron decoy"
      - x: 0.0
        y: 0.85
        w: 1.0
        h: 0.15
        label: "pond edge reflection"
```

Any detection whose bounding box center falls inside an exclusion zone is silently dropped before an event is recorded or any notification is sent.

---

### Arm/Disarm Scheduling

ScarGuard can automatically arm itself at dawn and disarm at dusk, or on any fixed schedule you define. The system checks the schedule every 60 seconds. A manual arm/disarm from the dashboard overrides the schedule until the next scheduled transition.

**Fixed-time schedule:**

```yaml
system:
  schedule:
    enabled: true
    arm_time: "06:00"    # arm at 6:00 AM
    disarm_time: "21:00" # disarm at 9:00 PM
    timezone: "America/New_York"  # set under system.timezone
```

**Solar-based schedule** (adapts automatically to sunrise/sunset throughout the year):

```yaml
system:
  timezone: "America/New_York"
  schedule:
    enabled: true
    use_solar: true
    latitude: 39.9526    # decimal degrees
    longitude: -75.1652  # decimal degrees, negative = west
```

With solar mode enabled, the system arms at sunrise and disarms at sunset each day. No manual adjustment needed as daylight hours shift with the seasons.

The **Dashboard** shows the current arm state, today's scheduled arm/disarm times, and the time until the next transition.

Schedule settings are available in the web UI under **Settings → Schedule**.

---

### Training Pipeline

ScarGuard includes a complete pipeline for collecting labeled training data from live detections and using it to improve the YOLO model. The workflow is:

**Step 1 — Label detections as they happen**

On the **Events** page, each detection has feedback buttons:
- **Correct** — the detection is accurate (right species, right bounding box)
- **False Positive** — the system fired on something that isn't a target
- **Wrong Class** — the detection is real, but the class is wrong (select the correct class from the dropdown)

Unreviewed events have a distinct visual treatment in the table. Feedback can be changed after initial submission.

**Step 2 — Check dataset quality**

Open **Admin → Training Data** to see:
- How many labeled events exist per class
- Bar chart of class distribution
- Count of false positives and wrong-class corrections
- A warning if any class has fewer than 500 confirmed samples (the minimum for reliable fine-tuning)

Use the date range filter to limit the dashboard to a specific collection period.

**Step 3 — Export the dataset**

Click **Export Dataset** in the Training Data dashboard. This downloads a `.zip` with:
- `dataset/images/train/` — the detection snapshots
- `dataset/labels/train/` — YOLO-format annotation files (class index, normalized bounding box)
- `data.yaml` — class names and dataset structure for Ultralytics training

Only `feedback = correct` events are exported as positive training samples. `wrong_class` events are included with the corrected label as ground truth. False positives are excluded.

**Step 4 — Train a new model**

Run the included training script on a machine with a GPU (the Orin works, but a workstation GPU is faster for training):

```bash
cd training
python train.py \
  --data /path/to/dataset/data.yaml \
  --output heron-v2.pt \
  --base-model yolov8n.pt \
  --epochs 100
```

Run `python train.py --help` for the full CLI reference and recommended hyperparameters.

**Step 5 — Upload the trained model**

In the web UI, go to **Admin → Models** and upload the `.pt` file produced by training. It will appear in the model list.

**Step 6 — Evaluate before promoting**

Go to **Admin → Model Evaluation** and compare the new model against the current active model:
1. Select the current model and the candidate model
2. Choose a date range of labeled snapshots to test against
3. Click **Run Evaluation** — the detector runs both models on your GPU and streams progress
4. Review per-class precision, recall, and F1 scores side-by-side

If the new model wins, click **Promote** on the evaluation results page. This updates `scarguard.yml` and triggers a hot-reload — no restart required.

---

### Model Management

ScarGuard supports three model formats:
- `.pt` — PyTorch/Ultralytics YOLOv8 (best for flexibility and training)
- `.engine` — TensorRT-optimized (best inference speed on the Jetson)
- `.onnx` — ONNX Runtime (cross-platform, moderate speed)

**Uploading a model:**

Go to **Admin → Models**. Drag and drop or browse for your model file. The file is stored in the `models/` volume and immediately available for selection.

**Inspecting a model's class list (v0.13.4+):**

Each row on the Models page has a **Show classes** button that expands to reveal the class names embedded in the model (`model.names`). Copy-to-clipboard gives you a comma-separated list for pasting into `detection.target_classes` or a camera's `detect_classes`. TensorRT `.engine` files compiled without embedded names show a warning pointing back at the source `.pt`.

**Setting the active model:**

Update `detection.model_path` in `scarguard.yml` (or via **Settings → Detection** in the UI) and click Save. The detector hot-reloads the new model within ~10 seconds — no container restart needed.

```yaml
detection:
  model_path: /models/heron-v2.engine
  target_classes:
    - great_blue_heron
    - green_heron
  confidence_threshold: 0.45
```

**Converting to TensorRT** for best performance on the Orin:

```bash
# Run inside the detector container
docker compose exec detector python -c "
from ultralytics import YOLO
model = YOLO('/models/heron-v2.pt')
model.export(format='engine', device=0, half=True)
"
```

The exported `.engine` file will appear in `models/` alongside the `.pt`.

---

### User Management

ScarGuard requires authentication — all web UI routes and API endpoints are gated behind login. The first admin account is created during `setup.sh`.

**Adding users:**

Go to **Admin → Users** and click **Add User**. Usernames and passwords are required (minimum 8 characters). Check **Admin** to grant full access including user management, training, and config editing. Non-admin users can view the dashboard and events but cannot change configuration.

**Managing existing users:**

From the Users page you can:
- **Disable** a user account (they can't log in but their history is preserved)
- **Change password** (admins can change any user's password; users can change their own)
- **Delete** a user permanently

**Login security:**

By default, accounts are locked for 15 minutes after 5 consecutive failed login attempts. You can tune this in `scarguard.yml`:

```yaml
system:
  auth:
    enabled: true
    max_login_attempts: 5
    lockout_duration_minutes: 15
    session_timeout_hours: 24
```

These settings are also available in the web UI under **Settings → Authentication**.

---

### API Tokens

API tokens allow programmatic access to ScarGuard's REST API without a browser session — useful for scripts, Home Assistant automations, or any external integration.

**Creating a token:**

Go to **Admin → Users** and click **Create API Token**. Give it a descriptive name (e.g. "home-assistant-integration"). The token is shown exactly once — copy it and store it securely.

**Using a token:**

Include the token as a Bearer header on any API request:

```bash
curl -H "Authorization: Bearer YOUR_TOKEN_HERE" \
  http://your-orin/events
```

The Caddy reverse proxy listens on port 80 by default (or your configured `HTTP_PORT` / `HTTPS_PORT`) — there is no need to target the internal web container port directly.

**Key endpoints available with Bearer auth:**
- `/events` — detection event log (filterable by camera, class, date)
- `/config` — read or update system configuration
- `/about` — version, build date, component health
- `/feed/stream` — SSE stream of live annotated detection snapshots
- `/events/stream` — SSE stream of live detection events
- `/admin/stats/stream` — SSE stream of system resource metrics
- `/admin/logs/stream` — SSE stream of service logs

FastAPI's built-in Swagger UI is also available at `/docs` after login, providing an interactive API reference.

**Revoking a token:**

Tokens are listed under **Admin → Users → API Tokens**. Click **Revoke** next to the token you want to invalidate.

---

### System Stats & Logs

**Stats page** — **Admin → System Stats**

Shows real-time system resource utilization, updated on a configurable interval (default: 5 seconds):
- CPU usage (%)
- RAM used / total
- CPU temperature
- GPU usage (%) and GPU memory (used / total)
- GPU temperature
- Rolling 10-minute mini-charts for CPU and GPU usage
- Per-camera inference FPS and average latency

GPU stats are auto-detected from the Jetson sysfs, tegrastats, or nvidia-smi — no manual configuration needed.

To change the update interval:

```yaml
system:
  stats_interval: 10   # seconds, 1–60
```

**Logs page** — **Admin → Logs**

Live tail of container logs from the detector, web, and notifier services. Filter by:
- Service (detector / web / notifier)
- Log level (All / Debug+ / Info+ / Warning+ / Error only)
- Tail depth (200–2000 lines)

Auto-scroll and pause/resume controls keep the stream readable during a high-volume debugging session. The stream reconnects automatically if interrupted.

---

## Development Status

| Version | Description | Status |
|---------|-------------|--------|
| v0.1 | Detection engine (RTSP + YOLO + SQLite + Redis) | Complete |
| v0.2 | Notifications (Discord webhook + Email SMTP), web UI, CI/CD, hot-reload | Complete |
| v0.3 | Admin logs tab, SSL/TLS, snapshot retention, SSL config UI | Complete |
| v0.4 | Exclusion zones, enhanced event log, action rules, live feed, named channels, scheduling | Complete |
| v0.5 | GPU/CPU stats view (live metrics, per-camera FPS, rolling charts) | Complete |
| v0.6 | App security — session auth, first-run setup, user management, API tokens, lockout | Complete |
| v0.7 | Detection feedback, training data dashboard, YOLO export, training script, model evaluation | Complete |
| v0.8 | Per-camera models, named Docker volumes, CI PR build validation | Complete |
| v0.9 | Ntfy, visit tracking, camera health, metrics persistence, training nudge, config backup, on-demand snapshot, CI hardening | Complete |
| v0.10 | CI/CD pipeline hardening (compose smoke test, GPU/CPU benchmarks); x86/CUDA detector image with CPU fallback | Complete |
| v0.11 | Unified retention, scheduled digest reports, mobile-friendly admin menu, event pruning | Complete |
| v0.12 | HTML email, notification feedback tokens, config UI modes, health checks, Caddy TLS reverse proxy (Beta 1) | Complete |
| v0.12.3–v0.12.10 | Hardening patch cycle (Redis auth, FairLock, log-streamer sidecar, inference perf, viewer role, audit log, CodeQL) | Complete |
| v0.13.0 | Deterrent service MVP — Tuya Cloud control of sprinklers, lights, sirens, plugs | Complete |
| v0.13.1 | Deterrent web UI — actuation log, device status, test-fire, config UI | Complete |
| v0.13.2 | Review fixes, legacy notification key removal, doc cleanup | Complete |
| v0.13.3 | Per-camera deterrent scoping, confidence thresholds, UI tabs, latency instrumentation | Complete |
| v0.13.4 | Chip autocomplete, model-class introspection, Docker Hub auth | Complete |
| v0.13.5 | Dashboard deterrent widget, chip-picker z-index fix, compose-smoke port remap | Complete |
| v1.14.0 | GA hardening (Beta 3): actuation watchdog + reconciliation, HMAC Redis bus, encrypted secrets, bootstrap token, SSRF guard, rate limits, compose hardening, SQLite backup sidecar | In progress |

See [ROADMAP.md](ROADMAP.md) for planned features and [STATUS.md](STATUS.md) for a detailed breakdown of what's working.
