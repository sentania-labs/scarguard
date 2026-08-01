# Contributing to ScarGuard

Thanks for your interest in contributing. ScarGuard is a small project and we welcome bug reports, feature ideas, and pull requests.

## Getting Started

1. **Read the docs** — [README.md](README.md), [ROADMAP.md](ROADMAP.md), and [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md) cover what's built, what's planned, and how the system works.
2. **Check the roadmap** — Features 17–26 are planned and open for contribution. If you want to work on one, open an issue first so we can coordinate.
3. **Check existing issues** — your bug or idea may already be tracked.

## Reporting Bugs

Use the [bug report template](https://github.com/sentania-labs/scarguard/issues/new?template=bug_report.yml). Include:
- What happened and what you expected
- Steps to reproduce
- Your hardware (Jetson model, x86 specs, etc.)
- ScarGuard version (check the About page)
- Relevant logs (`docker compose logs` or Admin > Logs in the web UI)

## Suggesting Features

Use the [feature request template](https://github.com/sentania-labs/scarguard/issues/new?template=feature_request.yml). Focus on the problem you're trying to solve — we'll figure out the implementation together.

## Pull Requests

### Before You Start

- **Open an issue first** for anything non-trivial (new features, architectural changes). This avoids wasted effort if the approach doesn't fit.
- **Small PRs are better** — one feature or fix per PR. Don't bundle unrelated changes.
- Bug fixes and documentation improvements are always welcome without prior discussion.

### Development Setup

ScarGuard runs as a Docker Compose stack. For local development:

```bash
git clone https://github.com/sentania-labs/scarguard.git
cd scarguard
pip install ruff mypy types-PyYAML types-requests types-redis
```

You don't need a Jetson or GPU to work on the web service or notifier — only the detector requires GPU access.

### Code Standards

- **Python 3.11** across all services
- **Type hints** on all functions
- **Pydantic models** for data structures
- **Logging** via Python `logging` module, structured JSON output
- **No over-engineering** — this is a pond guardian, not a distributed platform

### Linting & Type Checking

These must pass before submitting a PR (mirrors CI):

```bash
# Ruff — all services
ruff check services/detector/src services/web/src services/notifier/src services/deterrent/src services/backup/src shared

# mypy — web
MYPYPATH=services/web/src:shared \
  python3 -m mypy services/web/src shared --ignore-missing-imports --explicit-package-bases

# mypy — notifier
MYPYPATH=services/notifier/src:shared \
  python3 -m mypy services/notifier/src shared --ignore-missing-imports --explicit-package-bases

# mypy — deterrent
MYPYPATH=services/deterrent/src:shared \
  python3 -m mypy services/deterrent/src shared --ignore-missing-imports --explicit-package-bases

# mypy — backup
MYPYPATH=services/backup/src:shared \
  python3 -m mypy services/backup/src shared --ignore-missing-imports --explicit-package-bases
```

### Self-Review Protocol (AI-assisted changes)

When a non-trivial change is made by an AI collaborator, run a
self-review via subagent before considering the task done:

1. Run `git diff HEAD` to capture all uncommitted changes.
2. Spawn a review subagent with this prompt:
   > "Review the following diff for: correctness, error handling,
   > consistency with the scarguard Python style (type hints,
   > Pydantic models, structured logging), and any risks specific to
   > a Jetson/Docker/RTSP environment. Be direct about issues. Diff:
   > [paste diff]"
3. Address any issues flagged before marking the task complete.

**Applies to:**

- Any change to `services/` (any service container, including
  trainer and training-controller)
- Any change to `shared/`
- Any change to `docker-compose.yml` or a Dockerfile
- Config schema changes

**Doesn't apply to:**

- Documentation-only changes
- Comment / whitespace changes
- Dependency bumps with no logic changes

### PR Process

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Run linting and type checks (see above)
4. Push and open a PR against `main`
5. Describe what your PR does and why — link the related issue if there is one

CI will run linting, type checking, tests, and image builds on your PR automatically.

## Architecture Notes

If you're diving into the code, here's the lay of the land:

| Service | What it does | Can develop without GPU? |
|---------|-------------|------------------------|
| `web` | FastAPI + Jinja UI, SQLite, config management | Yes |
| `notifier` | Redis subscriber, dispatches to Discord/email/webhooks | Yes |
| `detector` | RTSP ingestion, YOLO inference, event publishing | No (needs NVIDIA GPU) |
| `redis` | Internal message bus | N/A |

Services communicate via Redis pub/sub. All config lives in a single `scarguard.yml`. See [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md) for the full schema.

## Design Decisions

These are intentional and should not be changed without discussion:

1. **Docker Compose** is the deployment target — no Kubernetes
2. **Single config file** (`scarguard.yml`) — no per-service configs
3. **Redis pub/sub** for IPC — no Kafka or RabbitMQ
4. **SQLite** — no Postgres (single device, one writer)
5. **Python 3.11** — pinned to match L4T base image compatibility
6. **Snapshots are files on disk** — no blob store or database storage

See [CLAUDE.md](CLAUDE.md) for the full list.

## License

ScarGuard is [MIT licensed](LICENSE). By contributing, you agree that your contributions will be licensed under the same terms.
