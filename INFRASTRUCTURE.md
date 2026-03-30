# ScarGuard — Infrastructure

Doc last verified: 2026-03-24

## Repository Structure

```
scarguard/
├── docker-compose.yml
├── docker-compose.gpu.yml
├── BENCHMARKS.md
├── setup.sh
├── pyproject.toml
├── .env.example
├── config/
│   └── scarguard.example.yml
├── models/                          # YOLO model files (.pt, .engine)
├── data/
│   └── scarguard.db
├── services/
│   ├── detector/                    # RTSP ingestion + YOLO inference
│   │   ├── Dockerfile               # Jetson/L4T (ARM64)
│   │   ├── Dockerfile.x86           # x86 CUDA+CPU
│   │   ├── requirements.txt
│   │   └── src/
│   │       ├── main.py
│   │       ├── stream.py
│   │       ├── detector.py
│   │       ├── events.py
│   │       ├── publisher.py
│   │       ├── cleanup.py           # Snapshot retention / daily pruning daemon
│   │       └── config_watcher.py
│   ├── web/                         # FastAPI + Jinja web UI
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── src/
│   │       ├── start.py             # Startup script: reads ssl config, launches uvicorn
│   │       ├── config_model.py
│   │       ├── config_store.py
│   │       ├── db.py
│   │       ├── main.py
│   │       ├── routes/
│   │       │   ├── __init__.py
│   │       │   ├── about.py
│   │       │   ├── admin.py         # Admin logs tab: SSE log stream from Docker containers
│   │       │   ├── config.py
│   │       │   ├── dashboard.py
│   │       │   ├── events.py
│   │       │   ├── feed.py
│   │       │   └── models.py
│   │       ├── static/
│   │       │   ├── config.js
│   │       │   └── style.css
│   │       └── templates/
│   │           ├── about.html
│   │           ├── base.html
│   │           ├── config.html
│   │           ├── dashboard.html
│   │           ├── events.html
│   │           ├── feed.html
│   │           ├── logs.html        # Admin logs page (service selector, level filter, SSE)
│   │           ├── models.html
│   │           └── partials/
│   │               ├── arm_badge.html
│   │               └── event_rows.html
│   └── notifier/
│       ├── Dockerfile
│       ├── requirements.txt
│       └── src/
│           ├── config_watcher.py
│           ├── discord.py
│           ├── email_notifier.py
│           ├── main.py
│           └── notification_queue.py
├── shared/
│   └── models.py                    # Shared Pydantic data models
├── infra/
│   ├── orin-runner/
│   │   ├── Dockerfile
│   │   └── entrypoint.sh
│   ├── docker-compose.runner.yml
│   └── orin-setup.sh
└── .github/
    └── workflows/
        └── ci.yml
```

## Container Base Images

- **detector (Jetson):** `dustynv/l4t-pytorch:r36.4.0` (CUDA, cuDNN, PyTorch, TensorRT). Compatible with L4T r36.4.7. GPU via NVIDIA Container Runtime (`docker-compose.gpu.yml` override).
- **detector (x86):** `pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime` (CUDA, cuDNN, PyTorch). Uses GPU when NVIDIA runtime available, falls back to CPU. Published as `scarguard-detector-x86`. The PyTorch tag is parameterized via `ARG PYTORCH_TAG` in `Dockerfile.x86` — override with `--build-arg PYTORCH_TAG=<tag>` to test a different version. Bump the default when cutting a release.
- **web and notifier:** `python:3.11-slim` — no GPU needed.
- **caddy:** `caddy:2-alpine` + Python for config parsing.

## Named Volumes

All application data is stored in Docker named volumes (not bind mounts). This simplifies deployment and makes the project consumable without host-path dependencies.

| Volume | Service(s) | Access | Purpose |
|--------|-----------|--------|---------|
| `scarguard-config` | all | rw (web), ro (detector, notifier) | `scarguard.yml` config + SSL certs (`certs/` subdirectory) |
| `scarguard-data` | detector, web, notifier | rw (detector, web), ro (notifier) | SQLite DB (`scarguard.db`, `auth.db`) + snapshots |
| `scarguard-models` | detector, web | rw (web — model upload), ro (detector) | YOLO model files (`.pt`, `.engine`) |
| `scarguard-notifier` | notifier | rw | Notifier retry queue state |
| `redis-data` | redis | rw | Redis persistence |

Additionally, the Docker socket is bind-mounted into the web container for Admin Logs:

| Bind mount | Service | Purpose |
|------------|---------|---------|
| `/var/run/docker.sock:/var/run/docker.sock:ro` | web | Docker log streaming for Admin Logs tab |

> **Security note:** Mounting `/var/run/docker.sock` gives the web container host-root equivalent access to the Docker daemon. This is gated behind authentication (Feature 9).

### Migrating from bind mounts (v0.7 and earlier)

Installations that used `DATA_DIR` bind mounts can migrate to named volumes using the one-time `migrate-to-volumes.sh` script (not tracked in git — generate or download it for the upgrade).

## Container Registry

All application images pushed to **GitHub Container Registry (ghcr.io)**. Orin pulls from GHCR for production.

## Hardware (Reference Setup)

ScarGuard works with any RTSP cameras and any Docker host with an NVIDIA GPU. The reference deployment uses:

- **Compute:** NVIDIA Jetson Orin Nano, JetPack 6.2.1 (L4T 36.4.7), hardwired into UniFi fabric
- **Cameras:** 2x UniFi cameras (G3 Flex + G5 Flex) streaming RTSP — any RTSP camera works
- **Deterrence (future):** Physical deterrence hardware (solenoid valves, relays) will be managed by companion project "Scar's Revenge," which receives webhook notifications from ScarGuard
- **Network:** Host and cameras on the same LAN with layer 2 connectivity to RTSP sources

> **Portability:** Two detector images: `scarguard-detector` (Jetson/L4T ARM64) and `scarguard-detector-x86` (x86 CUDA+CPU). Web, notifier, and caddy images are multi-arch. `setup.sh` auto-selects the correct detector image via `DETECTOR_IMAGE` env var.

## CI/CD Strategy

### Runners

- **x86 runners (existing org runners):** Lint, type checking, pytest for web + notifier, build and push web/notifier/caddy/detector-x86 images to GHCR, compose smoke test
- **Orin runner (self-hosted, containerized):** Build Jetson detector image (ARM64 + L4T base), GPU smoke test + inference benchmark

### x86 Runner Details
- Containerized GitHub Actions runner on ubuntu24 host, Dockerfile managed out of band
- Docker socket mount from host — runner issues Docker commands against host daemon
- Labels: `self-hosted`, `linux`, `X64`, `docker`

### Orin Runner Details
- Containerized GitHub Actions runner on the Orin (ARM64 Dockerfile in `infra/orin-runner/`)
- Docker socket mount from host — runner issues Docker commands against host daemon
- GPU accessible because builds/tests run on host Docker (not nested)
- Labels: `self-hosted`, `linux`, `arm64`, `jetson`

### Build & Deploy Flow

```
Push to main / PR
  ├── x86 runners:
  │   ├── Lint + type check (all services)
  │   ├── pytest (web, notifier — no GPU needed)
  │   ├── Build web/notifier/caddy images (multi-arch amd64+arm64)
  │   ├── Build detector-x86 image + CPU inference benchmark
  │   └── Compose smoke test (full stack, CPU mode)
  │
  └── Orin runner:
      ├── Build Jetson detector image (ARM64 + L4T base)
      └── GPU smoke test + inference benchmark

Tag push (release)
  ├── x86 runners:
  │   ├── Build + push web/notifier/caddy to ghcr.io
  │   └── Build + push detector-x86 to ghcr.io + CPU benchmark
  │
  ├── Orin runner:
  │   └── Build + push detector to ghcr.io + GPU benchmark
  │
  └── Post-release:
      ├── Append benchmarks to BENCHMARKS.md
      └── Create GitHub Release with image table
```

### Runner Image Updates
Currently manual: SSH into Orin, rebuild runner image, restart container.

## Platform Selection (Environment Variables)

`setup.sh` auto-detects the platform and writes these to `.env`:

| Variable | Purpose | Jetson value | x86 + GPU value | x86 CPU-only value |
|----------|---------|-------------|-----------------|-------------------|
| `DETECTOR_IMAGE` | Detector container image | `ghcr.io/.../scarguard-detector` | `ghcr.io/.../scarguard-detector-x86` | `ghcr.io/.../scarguard-detector-x86` |
| `COMPOSE_FILE` | Compose files to load | `docker-compose.yml:docker-compose.gpu.yml` | `docker-compose.yml:docker-compose.gpu.yml` | `docker-compose.yml` |

The `docker-compose.gpu.yml` override adds `runtime: nvidia` and NVIDIA environment variables to the detector service. Without it, the detector runs on CPU.

## Host Prerequisites

Before running any containers, the Orin host needs:

1. Docker Engine installed
2. NVIDIA Container Toolkit installed and configured
3. `nvidia-container-runtime` set as default runtime
4. GitHub Actions runner container running (see `infra/` directory)

Use `infra/orin-setup.sh` for steps 1-3.

Test with:
```bash
docker run --rm --runtime=nvidia --gpus all dustynv/l4t-pytorch:r36.4.0 python3 -c "import torch; print(torch.cuda.is_available())"
```
