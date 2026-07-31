# ScarGuard — Infrastructure

Doc last verified: 2026-04-20

## Repository Structure

```
scarguard/
├── docker-compose.yml
├── docker-compose.gpu.yml           # NVIDIA Container Runtime override
├── BENCHMARKS.md
├── setup.sh
├── pyproject.toml
├── .env.example
├── config/
│   ├── scarguard.example.yml
│   ├── Caddyfile.template           # REFERENCE ONLY — active Caddyfile is rendered by caddy-entrypoint.sh
│   └── caddy-entrypoint.sh          # Reads tls/* from scarguard.yml, generates Caddyfile at runtime
├── services/
│   ├── detector/                    # RTSP ingestion + YOLO inference
│   │   ├── Dockerfile               # Jetson/L4T (ARM64)
│   │   ├── Dockerfile.x86           # x86 CUDA+CPU (PYTORCH_TAG build arg)
│   │   ├── entrypoint.sh            # Volume ownership fix + gosu drop
│   │   ├── requirements.txt
│   │   ├── src/
│   │   │   ├── main.py
│   │   │   ├── stream.py
│   │   │   ├── detector.py
│   │   │   ├── events.py
│   │   │   ├── publisher.py
│   │   │   ├── cleanup.py           # Snapshot retention / daily pruning daemon
│   │   │   ├── camera_health.py     # Online/offline tracking + alerts
│   │   │   ├── evaluator.py         # Model evaluation runner (SSE)
│   │   │   ├── fair_lock.py         # FIFO inference lock (cross-camera fairness)
│   │   │   ├── metrics_store.py     # SQLite system_metrics writer
│   │   │   ├── model_pool.py        # Ref-counted YOLO model cache
│   │   │   ├── scheduler.py         # Arm/disarm (fixed-time + solar)
│   │   │   ├── snapshot_grabber.py  # On-demand RTSP snapshot over Redis req/resp
│   │   │   ├── stats_collector.py   # CPU/GPU/RAM/FPS collector
│   │   │   └── visit_tracker.py     # Detection → visit session grouping
│   │   └── tests/
│   ├── web/                         # FastAPI + Jinja web UI
│   │   ├── Dockerfile
│   │   ├── entrypoint.sh            # Volume ownership fix + gosu drop
│   │   ├── requirements.txt
│   │   ├── src/
│   │   │   ├── start.py             # Startup: launches uvicorn on 8080 (Caddy proxies in)
│   │   │   ├── main.py
│   │   │   ├── config_model.py      # Pydantic structured config schema
│   │   │   ├── config_store.py      # Atomic YAML load/save + stale-key stripping
│   │   │   ├── config_backup.py
│   │   │   ├── config_redact.py     # Viewer-role secret masking
│   │   │   ├── actuation_db.py      # Read-only deterrent.db access
│   │   │   ├── audit.py             # audit_events table + writer
│   │   │   ├── auth.py              # bcrypt + session + lockout + roles
│   │   │   ├── route_auth.py        # Login/logout/setup routes
│   │   │   ├── db.py
│   │   │   ├── routes/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── about.py
│   │   │   │   ├── actuations.py    # Deterrent actuation log + SSE
│   │   │   │   ├── admin.py         # Admin dashboard incl. logs tab
│   │   │   │   ├── audit_log.py     # /admin/audit-log viewer
│   │   │   │   ├── auth.py          # Auth-gated helpers
│   │   │   │   ├── config.py        # Structured + raw YAML config editor
│   │   │   │   ├── dashboard.py
│   │   │   │   ├── deterrent.py     # Device status, test-fire, defaults UI
│   │   │   │   ├── events.py
│   │   │   │   ├── feedback.py
│   │   │   │   ├── models.py
│   │   │   │   ├── snapshot.py      # Token-scoped snapshot serving
│   │   │   │   ├── stats.py         # System stats SSE
│   │   │   │   ├── training.py      # Training data dashboard + export
│   │   │   │   └── users.py         # User CRUD, API tokens
│   │   │   ├── static/
│   │   │   │   ├── config.js
│   │   │   │   └── style.css
│   │   │   └── templates/
│   │   │       ├── about.html
│   │   │       ├── actuations.html
│   │   │       ├── audit_log.html
│   │   │       ├── backups.html
│   │   │       ├── base.html
│   │   │       ├── config.html
│   │   │       ├── dashboard.html
│   │   │       ├── deterrent.html
│   │   │       ├── evaluate.html
│   │   │       ├── events.html
│   │   │       ├── feedback.html
│   │   │       ├── login.html
│   │   │       ├── logs.html
│   │   │       ├── models.html
│   │   │       ├── setup.html
│   │   │       ├── stats.html
│   │   │       ├── training.html
│   │   │       ├── users.html
│   │   │       ├── visits.html
│   │   │       └── partials/
│   │   │           ├── arm_badge.html
│   │   │           └── event_rows.html
│   │   └── tests/
│   ├── notifier/
│   │   ├── Dockerfile
│   │   ├── entrypoint.sh
│   │   ├── requirements.txt
│   │   ├── src/
│   │   │   ├── main.py
│   │   │   ├── discord.py
│   │   │   ├── email_notifier.py
│   │   │   ├── webhook.py
│   │   │   ├── ntfy.py
│   │   │   ├── notification_queue.py
│   │   │   ├── snapshot_utils.py
│   │   │   ├── digest.py            # Daily/weekly/monthly summary formatter
│   │   │   ├── digest_db.py
│   │   │   └── digest_scheduler.py
│   │   └── tests/
│   ├── deterrent/                   # Physical deterrence via Tuya Cloud API
│   │   ├── Dockerfile
│   │   ├── entrypoint.sh
│   │   ├── requirements.txt
│   │   ├── src/
│   │   │   ├── main.py              # Redis subscriber + dispatcher
│   │   │   ├── cloud_controller.py  # tinytuya.Cloud wrapper
│   │   │   ├── randomizer.py        # Device selection / timing
│   │   │   ├── cooldown.py
│   │   │   ├── battery_monitor.py
│   │   │   ├── request_handler.py   # Redis req/resp for test-fire + status
│   │   │   ├── actuation_db.py      # SQLite writer for deterrent.db
│   │   │   └── actuation_models.py  # Pydantic actuation event schema
│   │   └── tests/
│   ├── caddy/                       # Reverse proxy (TLS termination)
│   │   └── Dockerfile               # Copies config/caddy-entrypoint.sh at build time
│   ├── log-streamer/                # Sidecar — tails Docker logs, publishes to Redis
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── src/
│   │       └── main.py
│   └── training-controller/         # Allowlisted detector stop/restore API (training profile)
│       ├── Dockerfile
│       ├── src/
│       │   └── main.py
│       └── tests/
├── shared/                          # Code shared across service containers
│   ├── models.py                    # Pydantic event models
│   ├── config_watcher.py            # Mtime-based config hot-reload helper
│   └── atomic_ref.py                # Thread-safe swap for hot-reload targets
├── training/
│   └── train.py                     # Standalone YOLO fine-tuning CLI
├── infra/
│   ├── orin-runner/
│   │   ├── Dockerfile
│   │   └── entrypoint.sh
│   ├── docker-compose.runner.yml
│   └── orin-setup.sh
└── .github/
    └── workflows/
        ├── ci.yml                   # Lint, type check, pytest (PR only)
        ├── build.yml                # Docker image builds (PR: full; main: cache only)
        ├── release.yml              # Build + push to GHCR on tag push
        └── cleanup.yml              # Weekly runner cleanup (all docker runners)
```

## Container Base Images

- **detector (Jetson):** `dustynv/l4t-pytorch:r36.4.0` (CUDA, cuDNN, PyTorch, TensorRT). Compatible with L4T r36.4.7. GPU via NVIDIA Container Runtime (`docker-compose.gpu.yml` override).
- **detector (x86):** `pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime` (CUDA, cuDNN, PyTorch). Uses GPU when NVIDIA runtime available, falls back to CPU. Published as `scarguard-detector-x86`. The PyTorch tag is parameterized via `ARG PYTORCH_TAG` in `Dockerfile.x86` — override with `--build-arg PYTORCH_TAG=<tag>` to test a different version. Bump the default when cutting a release.
- **web, notifier, deterrent, log-streamer, training-controller:** `python:3.11-slim` — no GPU needed.
- **caddy:** `caddy:2-alpine` + Python for config parsing.
- **redis:** `redis:7-alpine` (digest-pinned in `docker-compose.yml`).

### Non-root containers

The stateful application containers (detector, web, notifier, deterrent) create a `scarguard` system user and run the application as that user for defense in depth. Since `setup.sh` creates Docker volumes as root, each service has an `entrypoint.sh` that:

1. Starts as root
2. Fixes volume ownership (`chown -R scarguard:scarguard`) on first boot (sentinel file prevents slow re-chown on restarts with large snapshot volumes)
3. Drops to `scarguard` via `gosu` before executing the application

Each service uses a per-service sentinel file (`.ownership-fixed-{detector,web,notifier,deterrent}`) so services sharing the same volume don't skip each other's chown.

The `log-streamer` runs as a non-root user and reaches only the read endpoints
exposed by `docker-socket-proxy`. The training-controller runs as root inside its
minimal container so it can open the socket, but its API is detector-only.

CI bypasses the entrypoint with `--user root --entrypoint ""` for benchmark and test steps that need root write access.

## Named Volumes

All application data is stored in Docker named volumes (not bind mounts). This simplifies deployment and makes the project consumable without host-path dependencies.

| Volume | Service(s) | Access | Purpose |
|--------|-----------|--------|---------|
| `scarguard-config` | all application containers | rw (web, caddy-data), ro (detector, notifier, deterrent) | `scarguard.yml` config + manual TLS certs (`certs/` subdirectory) |
| `scarguard-data` | detector, web, notifier, deterrent, trainer | rw (detector, web, deterrent, trainer), ro (notifier) | SQLite DBs, snapshots, training workspace and durable logs |
| `scarguard-models` | detector, web, notifier | rw (web — model upload), ro (detector, notifier — storage size for digests) | YOLO model files (`.pt`, `.engine`) |
| `scarguard-notifier` | notifier | rw | Notifier retry queue state |
| `scarguard-caddy-data` | caddy | rw | Caddy Let's Encrypt cert storage |
| `scarguard-redis-data` | redis | rw | Redis persistence |
| `training-controller-state` | training-controller | rw | Detector lease ownership and crash-recovery state |

Docker access is mediated as follows (never mounted into web or trainer):

| Bind mount | Service | Purpose |
|------------|---------|---------|
| Docker socket via read-only proxy | log-streamer | Tails container logs and publishes to Redis |
| `/var/run/docker.sock:/var/run/docker.sock:ro` | training-controller (training profile only) | Narrow API stops/starts only the Compose detector service for exclusive training GPU access. The bind mode does not make Docker API writes read-only; the controller's allowlist is the boundary. |

> **Security note:** `web`, `trainer`, and `log-streamer` do not receive Docker
> write access. The opt-in training-controller is deliberately privileged but
> exposes only acquire/heartbeat/release/recover operations for the detector;
> it persists the exact container ID and never starts a detector it did not stop.

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

| Runner | Labels | Purpose |
|--------|--------|---------|
| `runner-docker` | self-hosted, linux, docker | x86 Docker builds |
| `runner-terraform` | self-hosted, linux, terraform, docker | x86 Docker builds |
| `runner-packer` | self-hosted, linux, packer, docker | x86 Docker builds |
| `runner-generic` / `-2` / `-3` | self-hosted, linux, generic | Lint, typecheck, pytest |
| `orin-nano` | self-hosted, linux, arm64, jetson | Jetson detector builds |

All x86 runners are containerized on an ubuntu24 host with Docker socket mount (DinD). The Orin runner uses `infra/orin-runner/` Dockerfile. GPU accessible because builds/tests run against the host Docker daemon.

### Orin GPU Lease (CI ↔ production coordination)

The Orin's 8GB unified memory holds exactly one GPU workload: the live detector, a training run, or a CI inference benchmark — the `orin-nano` runner is the same box as production. CI GPU steps (build.yml and release.yml detector jobs) therefore take a lease via `.github/scripts/ci-gpu-lease.sh` before touching the GPU:

1. **Acquire:** atomically claim the trainer heartbeat key (`SET NX EX 600`) — this waits out an active training run (up to 10 min, then fails with a re-run instruction) and blocks a new one from starting mid-benchmark; then pause the detector over the existing pause protocol (`shared/pause_protocol.py`) and wait for its ack.
2. **Release** (`if: always()`): drop the claim, resume the detector. Never fails the job.

Crash safety: the heartbeat key's TTL plus the detector's pause-timeout auto-resume guarantee a killed CI job cannot leave production paused. If the production stack (or detector) isn't running, acquire is a no-op. Redis access is via `docker exec` into the production redis container, so no Redis secret lives in CI.

Additionally, PRs only run the Jetson detector job when they touch detector-relevant paths (`detector-paths` job in build.yml) **and** carry the `orin-maintenance-approved` label; the PR trainer-image job requires the same label. Pushes to main and releases always run them. The label gate exists because these jobs run on the controlled production Jetson — see `docs/training-remediation-validation.md` for when to apply it.

### Build & Deploy Flow

```
PR to main (ci.yml + build.yml — full validation)
  ├── generic runners (parallel):
  │   ├── Lint (ruff — all services)
  │   ├── Type check (mypy — web, notifier, deterrent)
  │   ├── pytest — web
  │   ├── pytest — notifier
  │   ├── pytest — deterrent
  │   └── pytest — training runtime (trainer + lifecycle controller,
  │       fake children/cgroups/Docker backend — never touches the Orin)
  │
  ├── docker runners (parallel, one job per runner):
  │   ├── Build web image (multi-arch) + amd64 test + Trivy
  │   ├── Build notifier image (multi-arch) + amd64 test + Trivy
  │   ├── Build deterrent image (multi-arch) + amd64 test + Trivy
  │   ├── Build log-streamer image + Trivy
  │   ├── Build training-controller image (multi-arch) + Trivy
  │   ├── Build caddy image (multi-arch)
  │   └── Build detector-x86 image + CPU benchmark + Trivy
  │
  ├── Orin runner (only when the PR touches detector-relevant paths —
  │   services/detector/, shared/, tests/ci/, build.yml, .github/scripts/ —
  │   AND carries the orin-maintenance-approved label; trainer-image job
  │   needs the same label):
  │   └── Build detector image + GPU smoke test + benchmark
  │
  └── Compose smoke test (after all builds pass)

Merge to main (build.yml — cache warming only)
  ├── docker runners: Multi-arch builds only (warms GHA build cache)
  └── Tests, Trivy, and compose smoke test are SKIPPED (passed on PR)

Tag push (release.yml)
  ├── docker runners (parallel, one job per runner):
  │   ├── Build + push web to ghcr.io
  │   ├── Build + push notifier to ghcr.io
  │   ├── Build + push deterrent to ghcr.io
  │   ├── Build + push log-streamer to ghcr.io
  │   ├── Build + push training-controller to ghcr.io
  │   └── Build + push caddy to ghcr.io
  │
  ├── docker runner:
  │   └── Build + push detector-x86 to ghcr.io + CPU benchmark
  │
  ├── Orin runner:
  │   └── Build + push detector to ghcr.io + GPU benchmark
  │
  └── Post-release:
      ├── Append benchmarks to BENCHMARKS.md (auto-PR)
      └── Create GitHub Release with image table

Weekly (cleanup.yml — Sunday 03:00 UTC)
  ├── docker runners: system prune + builder cache prune (matrix hits all 3)
  └── Orin runner: system prune (no volume prune — preserves models)
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

## v1.14+ Additions

### Resource Limits

`docker-compose.yml` sets `mem_limit`, `cpus`, and `pids_limit` on
services as containment, not as a guarantee against host-global OOM. On the
Jetson's unified memory, the kernel may kill a training child while the trainer
container survives; admission control and persisted resource evidence handle
that case explicitly.
Defaults are sized for a Jetson Orin Nano:

| Service | mem | cpus | notes |
|---|---|---|---|
| detector | 3 GB | 4.0 | largest footprint — model + OpenCV + torch runtime |
| web | 512 MB | 1.0 | FastAPI + Jinja + CSP/HSTS middleware |
| notifier | 256 MB | 0.5 | read_only rootfs + tmpfs /tmp |
| deterrent | 256 MB | 0.5 | read_only rootfs + tmpfs /tmp |
| log-streamer | 128 MB | 0.25 | read_only rootfs + tmpfs /tmp |
| training-controller | 64 MB | 0.25 | detector-only Docker API boundary |
| trainer | 6 GB | 4.0 | limit includes child; unified-memory admission still applies |
| backup | 128 MB | 0.5 | periodic cycle, mostly idle |
| redis | 256 MB | 0.5 | + `--maxmemory 200mb --maxmemory-policy allkeys-lru` |
| caddy | 128 MB | 0.5 | keeps NET_BIND_SERVICE for 80/443 |
| docker-socket-proxy | 64 MB | 0.25 | tecnativa proxy; exposes only CONTAINERS + EVENTS |

Override via a `docker-compose.override.yml` if you're on beefier hardware.

Every service also runs with `security_opt: no-new-privileges:true`
and `cap_drop: [ALL]` (Caddy re-adds only `NET_BIND_SERVICE`).

### Trusted Proxies

`web` runs behind Caddy inside the Docker network. `forwarded_allow_ips`
defaults to the standard Docker bridge ranges:
`127.0.0.1,172.16.0.0/12,10.0.0.0/8,192.168.0.0/16`. Override via the
`SCARGUARD_TRUSTED_PROXIES` env var if your Docker install uses
non-default subnets or if you terminate TLS on an upstream proxy
beyond Caddy.

### Backup Architecture

The `backup` service runs SQLite's online-backup API against
`scarguard.db`, `auth.db`, and `deterrent.db` on a configurable
schedule (default 24h), writing gzipped output to
`/data/backups/{db}/{ISO-timestamp}.db.gz`. Retention is 14 daily +
8 weekly by default. Manual triggers via Redis or the admin UI at
`/admin/db-backups`. Full restore procedure lives in `BACKUP.md`.

The backup volume is the same `scarguard-data` Docker volume that
holds the source databases, so off-device replication is the
operator's responsibility — see the `BACKUP.md` guidance on rsync /
rclone / NAS copy.

### Secret Rotation Playbook

`/data/secret_key` (Fernet key used to encrypt sensitive YAML
fields — Tuya creds, SMTP passwords, webhook URLs, ntfy tokens):

1. `docker compose stop web notifier deterrent`
2. Back up the current config: copy `scarguard.yml` and
   `/data/secret_key` somewhere safe — you can't decrypt existing
   fields once the key is replaced.
3. Delete `/data/secret_key`. Start `web`; it will generate a new
   key on first boot.
4. Re-enter all encrypted fields via the admin UI (Tuya creds,
   channel secrets). Save each form.
5. Start `notifier` and `deterrent`.

A proper `scripts/rotate-secret-key.sh` that re-encrypts in place
is v1.15 work (tracked in ROADMAP.md).

`DETECTION_HMAC_KEY` (signs detection events on Redis),
`TRAINING_CONTROLLER_TOKEN` (authenticates the allowlisted detector lifecycle
API), and `REDIS_PASSWORD`:

1. Regenerate in `.env` — either re-run `setup.sh` (backfill path)
   or edit the values directly. See `.env.example` for generation
   one-liners.
2. `docker compose down && docker compose up -d`.

### Root CA in repo

`infra/orin-runner/sentania Lab Root 2.crt` is the public root
certificate for the Sentania Lab internal PKI. It's used by the
Orin-runner container to validate TLS to internal services. This
is the certificate *only*, not a private key. Intentional ship.
