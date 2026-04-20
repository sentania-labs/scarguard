# ScarGuard — AI-Powered Pond Wildlife Detection & Notification

Named after Scar (aka Kroger), a survivor fish badly injured by a heron who lived to tell the tale.

## What This Is

A containerized wildlife detection and notification system. Watches RTSP camera feeds for target species (herons, ducks, raccoons) and sends real-time notifications (Discord, email, webhooks) enabling downstream response to protect a backyard koi pond. ScarGuard handles detection, notification, and physical deterrence (sprinklers, lights, sirens via Tuya Cloud API). Works with any RTSP camera and any Docker host with an NVIDIA GPU.

## Tech Stack (Reference Setup)

- **Compute:** NVIDIA Jetson Orin Nano, JetPack 6.2.1 (L4T 36.4.7) — any NVIDIA GPU host works
- **Cameras:** 2x UniFi (G3 Flex + G5 Flex) via RTSP — any RTSP camera works
- **Services:** Docker Compose with 7 containers: Redis, Caddy, Detector, Web (FastAPI + Jinja), Notifier, Deterrent, Log-Streamer
- **Detection:** YOLO model on GPU, OpenCV RTSP ingestion
- **IPC:** Redis pub/sub as internal message bus
- **Database:** SQLite (single writer, no Postgres)
- **Language:** Python 3.11 across all services
- **CI/CD:** GitHub Actions → GHCR (x86 + Orin ARM64 self-hosted runners)
- **Config:** Single `scarguard.yml` in external data directory

## Design Decisions — Do Not Change Without Discussion

1. **Docker Compose is the deployment target.** No Kubernetes, no Swarm.
2. **Single config file** (`scarguard.yml`) is the source of truth. Don't fragment into per-service configs.
3. **Redis pub/sub** is the inter-service bus. No Kafka, RabbitMQ, or anything heavier.
4. **SQLite** is the database. Don't switch to Postgres — single Jetson, one writer.
5. **Python 3.11** across all services. Don't upgrade without testing against L4T base image.
6. **Notifications are working — don't refactor them.** Email and Discord are validated. New types are additive.
7. **RTSP streams will drop.** Detector must reconnect gracefully with exponential backoff. Never crash on a dropped stream.
8. **Snapshots are files on disk**, served by web service. Don't move to blob store or database.
9. **Prefer REST over MQTT** for external integrations. Keep the dependency surface small.
10. **Data directory is external to the repo.** `git pull` never touches user config, models, database, or snapshots.

## Development Guidelines

- **Type hints** on all functions
- **Pydantic models** for all data structures (events, config)
- **Logging:** Python `logging` module, structured JSON output, respect `log_level` from config
- **Error handling:** RTSP reconnect with backoff. Never crash on a dropped stream.
- **Testing:** pytest. Focus on detection logic and event pipeline. Don't over-test for MVP.
- **No over-engineering:** This is a pond guardian, not a distributed platform. Keep it simple.
- **Config Item UI:** All implemented config items should be configurable via UI. 
- **Documentation is not an afterthought:** Update the readme and supporting documents (STATUS, ROADMAP, INFRASTRUCTURE, CONFIG_REFERENCE) with updates and progress.

## Code Review Protocol

After completing any non-trivial code change (new feature, bug fix, refactor), you MUST perform a self-review via subagent before considering the task done.

### Mandatory Review Step
1. Run `git diff HEAD` to capture all uncommitted changes
2. Spawn a review subagent with this prompt:
   > "Review the following diff for: correctness, error handling, consistency with the scarguard Python style (type hints, Pydantic models, structured logging), and any risks specific to a Jetson/Docker/RTSP environment. Be direct about issues. Diff: [paste diff]"
3. Address any issues flagged before marking the task complete

### When This Applies
- Any changes to services/ (detector, web, notifier)
- Any changes to shared/models.py
- Any changes to docker-compose.yml or Dockerfiles
- Config schema changes

### When It Doesn't Apply
- Documentation only changes
- Comment/whitespace changes
- Dependency bumps with no logic changes

## Linting & Type Checking

Run these before considering any code change done (mirrors CI exactly):

```bash
# Ruff — all services (detector included; no GPU deps needed)
ruff check services/detector/src services/web/src services/notifier/src services/deterrent/src shared

# mypy — web (detector is excluded from CI: torch/opencv not available outside L4T)
MYPYPATH=services/web/src:shared \
  python3 -m mypy services/web/src shared --ignore-missing-imports --explicit-package-bases

# mypy — notifier
MYPYPATH=services/notifier/src:shared \
  python3 -m mypy services/notifier/src shared --ignore-missing-imports --explicit-package-bases

# mypy — deterrent
MYPYPATH=services/deterrent/src:shared \
  python3 -m mypy services/deterrent/src shared --ignore-missing-imports --explicit-package-bases
```

Required packages (if not already installed): `pip install ruff mypy types-PyYAML types-requests types-redis`

Both checks must pass with zero errors before the task is complete. Fix any issues rather than suppressing them.

## Reference Documents

Read these as needed — don't load them all for every task.

| Document | When to read |
|---|---|
| `ROADMAP.md` | Before starting new work, planning, or prioritizing |
| `ROADMAP_ARCHIVE.md` | When referencing implementation details of completed features (1–27) |
| `STATUS.md` | When debugging, assessing what's done, or checking known issues |
| `CONFIG_REFERENCE.md` | When touching config parsing, detection logic, RTSP, or `scarguard.yml` |
| `INFRASTRUCTURE.md` | When working on CI/CD, Docker, runners, deployment, or host setup |
