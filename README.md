# ScarGuard

An AI-powered pond wildlife deterrent system running on an NVIDIA Jetson Orin Nano. ScarGuard watches RTSP camera feeds for target species — primarily great blue herons — and triggers deterrent actions to protect a backyard koi pond.

Named after Scar (aka Kroger), a koi who survived a heron attack and lived to tell the tale.

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

### Valve Actuation (Phase 4)
- Four Orbit DC solenoid valves controlled via ESP32 over MQTT
- Randomized valve selection, spray duration, and inter-spray delay
- Independent cooldown to avoid over-saturating the yard

### Web UI
- Dashboard: arm/disarm toggle, live system status
- Detection event log with snapshot thumbnails and metadata
- Live camera feed with bounding box overlay (SSE)
- Model upload and hot-swap
- Config editor (reads/writes `scarguard.yml`)

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

## Quick Start

```bash
# 1. Copy and edit config
cp config/scarguard.yml.example config/scarguard.yml

# 2. Add your RTSP URLs and Discord webhook

# 3. Start the stack
docker compose up -d
```

The web UI is available at `http://<jetson-ip>:8080`.

---

## Development Status

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Detection engine (RTSP + YOLO + SQLite + Redis) | In progress |
| 2 | Notifications (Discord + email) | Planned |
| 3 | Web UI | Planned |
| 4 | Valve actuation (ESP32 + MQTT) | Planned |
