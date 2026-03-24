# ScarGuard — Infrastructure

## Repository Structure

```
scarguard/
├── docker-compose.yml
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
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── src/
│   │       ├── main.py
│   │       ├── stream.py
│   │       ├── detector.py
│   │       ├── events.py
│   │       └── publisher.py
│   ├── web/                         # FastAPI + Jinja web UI
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   ├── src/
│   │   │   ├── main.py
│   │   │   ├── routes/
│   │   │   │   ├── dashboard.py
│   │   │   │   ├── events.py
│   │   │   │   ├── config.py
│   │   │   │   ├── models.py
│   │   │   │   ├── about.py
│   │   │   │   └── admin.py
│   │   │   ├── api/
│   │   │   │   └── v1.py
│   │   │   └── db.py
│   │   ├── templates/
│   │   └── static/
│   └── notifier/
│       ├── Dockerfile
│       ├── requirements.txt
│       └── src/
│           ├── main.py
│           ├── discord.py
│           └── email.py
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

- **detector:** `dustynv/l4t-pytorch:r36.4.0` (CUDA, cuDNN, PyTorch, TensorRT). Compatible with L4T r36.4.7. GPU via NVIDIA Container Runtime (`runtime: nvidia` in Compose).
- **web and notifier:** `python:3.11-slim` — no GPU needed.

## Container Registry

All application images pushed to **GitHub Container Registry (ghcr.io)**. Orin pulls from GHCR for production.

## Hardware

- **Compute:** NVIDIA Jetson Orin Nano, JetPack 6.2.1 (L4T 36.4.7), hardwired into UniFi fabric
- **Cameras:** 2x UniFi cameras (G3 Flex + G5 Flex) streaming RTSP via UniFi Protect on UDM
- **Valves (future):** 4x Orbit DC solenoid valves (~6VDC, from Yard Enforcer), controlled by ESP32 + MOSFETs
- **Network:** All devices on same LAN behind UDM. Internal domain: `int.sentania.net`

## CI/CD Strategy

### Runners

- **x86 runners (existing org runners):** Lint, type checking, pytest for web + notifier, build and push non-GPU images to GHCR
- **Orin runner (self-hosted, containerized):** Build detector image (ARM64 + L4T base), GPU integration tests, full Compose smoke tests

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
Push to main
  ├── x86 runners:
  │   ├── Lint + type check (all services)
  │   ├── pytest (web, notifier — no GPU needed)
  │   └── Build + push web/notifier images to ghcr.io
  │
  └── Orin runner:
      ├── Build detector image locally (ARM64 + L4T base)
      ├── Run GPU smoke test (load model, single frame inference)
      └── Push detector image to ghcr.io
```

### Runner Image Updates
Currently manual: SSH into Orin, rebuild runner image, restart container.

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
