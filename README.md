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

Optionally enable Discord notifications:

```yaml
notifications:
  discord:
    enabled: true
    webhook_url: "https://discord.com/api/webhooks/..."
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

#### Step 2 — Enable SSL in `scarguard.yml`

```yaml
ssl:
  enabled: true
  cert_path: /certs/cert.pem   # container-internal path; do not change unless you
  key_path: /certs/key.pem     # also change the volume mount in docker-compose.yml
  https_only: false            # set true to disable plain HTTP on port 8080
```

#### Step 3 — Restart the web service

```bash
docker compose up -d web
```

The startup log will confirm:

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

**What changed in v0.3 and what it means for existing installs:**

| Change | Impact |
|--------|--------|
| New port mapping `${WEB_HTTPS_PORT:-8443}:8443` | Host port 8443 is now bound. Nothing listens on it until `ssl.enabled: true`, so existing HTTP deployments are unaffected. If 8443 is already in use on your host, add `WEB_HTTPS_PORT=<other>` to `.env` before running `docker compose up -d`. |
| New volume `${DATA_DIR}/config/certs:/certs:ro` | Docker Compose creates the empty host directory automatically. No action needed; the web service starts normally with an empty certs directory when SSL is disabled. |
| New volume `/var/run/docker.sock:/var/run/docker.sock:ro` | Enables the **Logs** tab in the web UI (live log tail from each service). Requires `docker compose up -d` to recreate the web container. If your Docker socket is at a non-standard path, the Logs tab will show a connection error — everything else is unaffected. |

---

## The Problem

Great blue herons are patient, methodical hunters. A single bird can empty a koi pond in a morning. Traditional deterrents — plastic owls, reflective tape — lose their effectiveness quickly as the birds habituate to them. What works is unpredictability: a deterrent that fires at random times, in random patterns, triggered only when a bird is actually present.

ScarGuard is that deterrent. It watches the pond around the clock, identifies threats with a YOLO vision model, and responds with randomized actions that keep wildlife guessing.

---

## Goals

- **Accurate, low-latency detection** — identify herons, ducks, and raccoons from live camera feeds with enough confidence to act, fast enough to matter
- **Randomized deterrence** — vary the response (which sprinkler valve fires, for how long, with what delay) so wildlife cannot pattern-match around it
- **Minimal false positives** — don't spray the yard every time a leaf blows past; confidence thresholds and cooldown windows keep the system from crying wolf
- **Always-on, self-healing** — RTSP streams drop; cameras reboot; the system must reconnect gracefully and resume without human intervention
- **Observable** — a web UI shows live status, recent detections with annotated snapshots, and configuration; Discord and email notifications keep the owner in the loop when away
- **Maintainable** — the whole stack runs in Docker Compose on the Jetson; deploying a new model or changing config should not require SSH access

---

## Targeted Capabilities

### Detection
- Real-time inference on live RTSP streams using a YOLO model running on the Jetson GPU (TensorRT-optimized)
- Target species: great blue heron, green heron, duck, raccoon (extensible via model swap)
- Per-class confidence thresholds and cooldown windows to suppress duplicate events
- Annotated snapshot saved on each detection event

### Notifications
- Discord webhook with snapshot image attached
- Email (SMTP) with snapshot attachment
- Configurable per-channel enable/disable and mention roles
- Planned support for webhook notfications


### Web UI
- Dashboard: arm/disarm toggle, live system status
- Detection event log with snapshot thumbnails and metadata
- Live camera feed with bounding box overlay (SSE)
- Model upload and hot-swap
- Config editor (reads/writes `scarguard.yml`)
- Admin logs tab: live log tail from each service, filterable by level
- HTTPS support: self-signed cert or custom cert, HTTP+HTTPS dual-listener

### Operations
- Containerized stack: `docker compose up` is the full deployment
- CI/CD via GitHub Actions — x86 runners handle linting, tests, and web/notifier image builds; a self-hosted runner on the Orin handles ARM64 detector builds and GPU smoke tests
- Images published to GitHub Container Registry (ghcr.io)

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
| `notifier` | Redis subscriber, Discord + email dispatch | `python:3.11-slim` |
| `redis` | Internal message bus | `redis:alpine` |

Services communicate over Redis pub/sub. All configuration lives in a single `config/scarguard.yml` file mounted into each container.

---

## Development Status

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Detection engine (RTSP + YOLO + SQLite + Redis) | Complete |
| 2 | Notifications (Discord + email) | Complete |
| 3 | Web UI | Complete |
| 4 | Web hook notifications | Planned |
| 5 | See Roadmap.md | Planned
