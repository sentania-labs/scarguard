# ScarGuard — AI-Powered Pond Wildlife Deterrent

## Project Overview

ScarGuard is a containerized wildlife detection and deterrent system that runs on an NVIDIA Jetson Orin Nano. It watches RTSP camera feeds for target species (primarily great blue herons, with expansion to ducks, raccoons, etc.) and triggers randomized deterrent actions (sprinkler valves, notifications) to protect a backyard koi pond.

Named after Scar (aka Kroger), a survivor fish who was badly injured by a heron and lived to tell the tale.

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
├── config/
│   └── scarguard.yml               # Single source of truth for all config
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
│   │   │   │   └── models.py       # Model upload/swap
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
        ├── ci.yml                   # Lint, test, build (x86 runners)
        └── deploy.yml               # Build + deploy to Orin (Orin runner)
```

### Service Communication

- **Between services:** Redis pub/sub as internal message bus. The detector publishes detection events; notifier and web UI subscribe.
- **Config:** All services read from mounted `config/scarguard.yml`. The web UI can write to it. Services watch for changes or require a restart.
- **Database:** SQLite at `data/scarguard.db`, shared volume between web and detector.

### Container Base Images

- **detector:** `dustynv/l4t-pytorch:r36.4.0` (provides CUDA, cuDNN, PyTorch, TensorRT). Compatible with L4T r36.4.7. GPU accessed via NVIDIA Container Runtime (`runtime: nvidia` in Compose).
- **web and notifier:** `python:3.11-slim` — no GPU needed.

### Container Registry

All application images are pushed to **GitHub Container Registry (ghcr.io)**. The Orin pulls from GHCR for production runs.

## Config File Format (scarguard.yml)

```yaml
system:
  armed: true
  log_level: info

cameras:
  - name: pond-north
    rtsp_url: "rtsp://172.16.0.1:7447/STREAM_TOKEN_1"
    enabled: true
    resolution: 720  # Use substream for inference
  - name: pond-south
    rtsp_url: "rtsp://172.16.0.1:7447/STREAM_TOKEN_2"
    enabled: true
    resolution: 720

detection:
  model_path: /models/best.engine  # Path inside container
  confidence_threshold: 0.25
  target_classes:
    - great_blue_heron
    - green_heron
    - duck
    - raccoon
  cooldown_seconds: 30        # Min time between events for same class
  frame_skip: 2               # Process every Nth frame

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

# Future: valve/MQTT actuation config
# valves:
#   mqtt_broker: "192.168.1.x"
#   mqtt_topic_prefix: "scarguard/valve"
#   spray_duration_min_sec: 3
#   spray_duration_max_sec: 8
#   valve_count: 4
#   randomize: true
#   cooldown_seconds: 60

redis:
  host: redis
  port: 6379
```

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

## CI/CD Strategy

### Runners

- **x86 runners (existing org runners):** Lint, type checking, pytest for web + notifier, build and push non-GPU images to GHCR
- **Orin runner (self-hosted, containerized):** Build detector image (ARM64 + L4T base), GPU integration tests, full Compose smoke tests

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
      ├── Stop detector service (free GPU)
      ├── Build detector image locally (ARM64 + L4T base)
      ├── Run GPU smoke test (load model, single frame inference)
      ├── Push detector image to ghcr.io
      ├── docker compose pull + up (deploy new images)
      └── Detector resumes with new image
```

### GPU Contention

The Orin has a single GPU shared between detection and CI. During builds:
- Image builds (`docker build`) do NOT use the GPU — no contention
- GPU tests require stopping the detector first
- Workflow stops detector → builds/tests → restarts detector
- Detection gap is brief and typically during non-peak hours (herons hunt dawn/dusk)

### Runner Image Updates

Currently manual: SSH into Orin, rebuild runner image, restart container. Automating via SSH from x86 runners is a future improvement (requires deploy keys and GitHub secrets).

## MVP Phases

### Phase 1: Detection Engine
- [ ] Dockerized detector service — single RTSP stream
- [ ] YOLO model loading and inference
- [ ] Detection event logging to SQLite
- [ ] Event publishing to Redis
- [ ] Snapshot image capture on detection

### Phase 2: Notifications
- [ ] Notifier service subscribes to Redis
- [ ] Discord webhook with snapshot image
- [ ] Email with snapshot attachment

### Phase 3: Web UI
- [ ] Dashboard: arm/disarm toggle, live status
- [ ] Detection event log with snapshot thumbnails
- [ ] Model upload (saves to /models, restarts or hot-reloads detector)
- [ ] Config editor (reads/writes scarguard.yml)
- [ ] Live camera feed with bounding box overlay (SSE or WebSocket)

### Phase 4 (Post-MVP): Valve Actuation
- [ ] MQTT publisher in detector or separate service
- [ ] ESP32 + ESPHome valve controller
- [ ] Randomized spray patterns (valve selection, duration, delay)
- [ ] Valve config in scarguard.yml and web UI

### Future Improvements
- [ ] SMS/iMessage notifications
- [ ] Automated Orin runner updates via SSH from x86 runners
- [ ] Second Orin or AGX as dedicated build runner
- [ ] Multi-model support (seasonal species profiles)

## Development Guidelines

- **Python 3.11** across all services
- **Type hints** on all functions
- **Pydantic models** for all data structures (events, config)
- **Logging:** Python `logging` module, structured JSON output, respect `log_level` from config
- **Error handling:** RTSP streams will drop — detector must reconnect gracefully with backoff
- **Testing:** pytest. Focus on detection logic and event pipeline. Don't over-test for MVP.
- **No over-engineering:** This is a pond guardian, not a distributed platform. Keep it simple.

## RTSP Notes

- UniFi Protect RTSP must be enabled per-camera in the Protect UI on the UDM
- RTSP URL format: `rtsp://172.16.0.1:7447/<stream_token>`
- Use 720p substream for inference — 4K wastes GPU cycles
- OpenCV `VideoCapture` handles RTSP natively; set `cv2.CAP_PROP_BUFFERSIZE` to 1 to reduce frame lag
- Camera models: G3 Flex and G5 Flex (G3 may be replaced with another G5)

## Host Prerequisites

Before running any containers, the Orin host needs:
1. Docker Engine installed
2. NVIDIA Container Toolkit installed and configured
3. `nvidia-container-runtime` set as default runtime
4. GitHub Actions runner container running (see `infra/` directory)

Use `infra/orin-setup.sh` for steps 1-3. Test with: `docker run --rm --runtime=nvidia --gpus all dustynv/l4t-pytorch:r36.4.0 python3 -c "import torch; print(torch.cuda.is_available())"`