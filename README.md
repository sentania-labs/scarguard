# ScarGuard

An AI-powered pond wildlife deterrent system running on an NVIDIA Jetson Orin Nano. ScarGuard watches RTSP camera feeds for target species — primarily great blue herons — and triggers deterrent actions to protect a backyard koi pond.

Named after Scar (aka Kroger), a koi who survived a heron attack and lived to tell the tale.

---

## Deploy on Jetson Orin Nano

> **You don't need to build anything.** Pre-built images are published to GitHub Container Registry automatically. Just clone, configure, and run.

### Prerequisites

- Jetson Orin Nano running JetPack 6.x (L4T 36.x)
- Internet connection (to pull images from ghcr.io)
- RTSP streams enabled in UniFi Protect for your cameras

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
- Check that Docker and the NVIDIA container runtime are installed (and offer to install them via `infra/orin-setup.sh` if not)
- Ask which port to use for the web UI (default: 8080)
- Create `config/scarguard.yml` from the example template
- Offer to generate a self-signed SSL certificate for HTTPS (stored in `${DATA_DIR}/config/certs/`)
- Offer to download a starter YOLO model (detects generic birds — good for testing the pipeline)
- Pull all pre-built images from GHCR
- Prompt you to create the initial admin account

### 3. Edit your configuration

```bash
# DATA_DIR is set in .env (default: /var/docker/scarguard)
nano /var/docker/scarguard/config/scarguard.yml
```

At minimum, set your camera RTSP URLs:

```yaml
cameras:
  - name: pond-north
    rtsp_url: "rtsp://YOUR_UDM_IP:7447/YOUR_STREAM_TOKEN"
    enabled: true
```

For a more complete initial setup, see the examples below or jump to [Feature Guides](#feature-guides).

**Cameras with exclusion zones** (suppress a static decoy or blind spot):

```yaml
cameras:
  - name: pond-north
    rtsp_url: "rtsp://YOUR_UDM_IP:7447/YOUR_STREAM_TOKEN"
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

    - name: sprinkler-valve
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
http://<your-orin-ip>:8080
```

You'll be redirected to the login page. Use the admin account created during setup.

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

### Changing the Web UI Port

If port 8080 is already in use, edit `.env`:

```bash
echo "WEB_PORT=9090" >> .env
docker compose up -d
```

---

### Enabling HTTPS

ScarGuard can serve the web UI over HTTPS on port 8443 alongside plain HTTP on 8080 (or HTTPS-only if you prefer).

#### Step 1 — Generate or provide a certificate

`setup.sh` offers to generate a self-signed certificate automatically. If you skipped that step or want to do it manually:

```bash
# Substitute your actual DATA_DIR path (from .env)
DATA_DIR=/var/docker/scarguard

mkdir -p "${DATA_DIR}/config/certs"
openssl req -x509 -newkey rsa:4096 \
    -keyout "${DATA_DIR}/config/certs/key.pem" \
    -out    "${DATA_DIR}/config/certs/cert.pem" \
    -days 3650 -nodes -subj "/CN=scarguard"
chmod 600 "${DATA_DIR}/config/certs/key.pem"
```

To use your own certificate (e.g. from Let's Encrypt or an internal CA), drop `cert.pem` and `key.pem` into `${DATA_DIR}/config/certs/` and proceed to Step 2. No container rebuild is needed — the directory is bind-mounted into the web container.

#### Step 2 — Enable SSL

**Option A — Web UI:** Open the Settings page, expand the **SSL / TLS** section, check **Enable HTTPS**, and click Save. The web service restarts automatically.

**Option B — Edit `scarguard.yml` directly:**

```yaml
ssl:
  enabled: true
  cert_path: /certs/cert.pem   # container-internal path; do not change unless you
  key_path: /certs/key.pem     # also change the volume mount in docker-compose.yml
  https_only: false            # set true to disable plain HTTP on port 8080
```

The web service detects SSL config changes and restarts automatically. The startup log will confirm:

```
INFO     start — Starting with SSL: cert=/certs/cert.pem key=/certs/key.pem https_only=False ...
```

#### Changing the HTTPS port

The default HTTPS port is 8443. To use a different port, add to `.env`:

```bash
echo "WEB_HTTPS_PORT=9443" >> .env
docker compose up -d web
```

#### Passphrase-protected private keys

If your private key has a passphrase, add to `scarguard.yml`:

```yaml
ssl:
  enabled: true
  keyfile_password: "your-passphrase"
```

> **Note:** Browsers will show a security warning for self-signed certificates. You can dismiss it, add a permanent exception, or install the cert into your OS/browser trust store for a clean experience on your local network.

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

    # Generic HTTP webhook — trigger a sprinkler valve
    - name: sprinkler-valve
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

> **Legacy keys:** `notifications.discord` and `notifications.email` flat keys are still supported for backward compatibility, but named channels under `notifications.channels` are the preferred format.

---

### Action Rules

Action rules control which notification channels fire for which detections. Without any rules defined, every detection triggers all enabled channels. With rules, you can route heron detections to the sprinkler valve while routing raccoon detections to Discord only.

Rules are defined at the top level of `scarguard.yml` and matched top-down — the first matching rule wins:

```yaml
action_rules:
  # Herons: notify Discord AND trigger the sprinkler valve
  - class: great_blue_heron
    actions: [pond-discord, sprinkler-valve]

  # Green herons: Discord only (no valve — too small to warrant it)
  - class: green_heron
    actions: [pond-discord]

  # Raccoons: alert the owner but don't spray
  - class: raccoon
    actions: [pond-discord, owner-email]

  # Anything else on the pond-north camera: log it only (no notification)
  - class: "*"
    camera: pond-north
    actions: []

  # Catch-all: everything else goes to Discord
  - class: "*"
    actions: [pond-discord]
```

Rule fields:
- `class` — the detected class name, or `"*"` to match any class
- `camera` — optional; if set, the rule only applies to detections from that camera
- `actions` — list of channel names to notify; empty list `[]` means log-only (silent)

The channel names in `actions` must match names defined in `notifications.channels`. Action rules are editable in the web UI under **Settings → Action Rules**.

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

See `training/README.md` for the full CLI reference, recommended hyperparameters, and Jetson-specific tips.

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
  http://your-orin:8080/events
```

This works for all REST endpoints including the SSE streams, the config API, and the arm/disarm endpoints.

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

## The Problem

Great blue herons are patient, methodical hunters. A single bird can empty a koi pond in a morning. Traditional deterrents — plastic owls, reflective tape — lose their effectiveness quickly as the birds habituate to them. What works is unpredictability: a deterrent that fires at random times, in random patterns, triggered only when a bird is actually present.

ScarGuard is that deterrent. It watches the pond around the clock, identifies threats with a YOLO vision model, and responds with randomized actions that keep wildlife guessing.

---

## Goals

- **Accurate, low-latency detection** — identify herons, ducks, and raccoons from live camera feeds with enough confidence to act, fast enough to matter
- **Randomized deterrence** — vary the response (which sprinkler valve fires, for how long, with what delay) so wildlife cannot pattern-match around it
- **Minimal false positives** — don't spray the yard every time a leaf blows past; confidence thresholds, cooldown windows, and exclusion zones keep the system from crying wolf
- **Always-on, self-healing** — RTSP streams drop; cameras reboot; the system must reconnect gracefully and resume without human intervention
- **Observable** — a web UI shows live status, recent detections with annotated snapshots, and configuration; Discord, email, and webhook notifications keep the owner in the loop
- **Maintainable** — the whole stack runs in Docker Compose on the Jetson; deploying a new model or changing config requires no SSH access

---

## Capabilities

### Detection
- Real-time inference on live RTSP streams using a YOLO model on the Jetson GPU (TensorRT-optimized)
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
- Action-rule routing — route heron detections to the sprinkler valve, raccoon detections to email only, etc.
- Retry with exponential backoff on transient failures

### Web UI
- **Dashboard** — arm/disarm toggle, latest detection, today's count, schedule status
- **Events** — paginated detection log with filters (camera, class, date range), snapshot overlays with bounding box rendering, real-time inserts via SSE, per-event feedback
- **Live Feed** — SSE-driven annotated detection snapshots with offline indicator and auto-reconnect
- **Settings** — full config editor (form-based and raw YAML), cameras, detection, notifications, channels, action rules, schedule, authentication, SSL
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
- HTTPS support — self-signed or custom cert, HTTP+HTTPS dual-listener, HTTPS-only option
- Session-based authentication — bcrypt passwords, configurable session timeout, login lockout
- Scheduled arm/disarm — fixed time or solar (sunrise/sunset via `astral`)
- Snapshot retention policy — configurable retention days, daily pruning

---

## Hardware

| Component | Details |
|-----------|---------|
| Compute | NVIDIA Jetson Orin Nano, JetPack 6.2.1 |
| Cameras | 2x UniFi cameras (G3 Flex + G5 Flex) via UniFi Protect RTSP |
| Valves | 4x Orbit DC solenoid valves, ESP32 + MOSFETs (planned) |
| Network | UniFi Dream Machine, internal domain `int.sentania.net` |

---

## Stack

| Service | Role | Base Image |
|---------|------|-----------|
| `detector` | RTSP ingestion, YOLO inference, event publishing | `dustynv/l4t-pytorch:r36.4.0` (CUDA + TensorRT) |
| `web` | FastAPI + Jinja UI, REST API, SQLite access | `python:3.11-slim` |
| `notifier` | Redis subscriber, Discord + email + webhook dispatch | `python:3.11-slim` |
| `redis` | Internal message bus | `redis:alpine` |

Services communicate over Redis pub/sub. All configuration lives in a single `scarguard.yml` file mounted into each container.

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

See [ROADMAP.md](ROADMAP.md) for planned features and [STATUS.md](STATUS.md) for a detailed breakdown of what's working.
