# ScarGuard Pre-Release Code Review
**Date:** 2026-04-03
**Commit:** 316bdc126f2aa1468fc3b8a93517fc27c4f8efc5
**Reviewed by:** Claude Code (5-agent audit)

## Executive Summary
ScarGuard is close to a solid v1.0 for a home/LAN deployment: the architecture is coherent (Compose + Redis + SQLite), major reliability concerns like RTSP reconnect/backoff are implemented, and there is thoughtful work around auth, CSRF, and config hot-reload. The codebase also shows good practical guardrails for hobby operations (retry queue for notifications, retention cleanup, and explicit comments around threat boundaries).

The main pre-release risk areas are operational hardening gaps and a few scaling bottlenecks rather than core correctness. The largest concerns are: (1) Docker socket exposure in the web container, (2) missing service healthchecks in Compose, (3) unpinned dependency surfaces (Python + Docker base/action tags), and (4) a likely broken Compose smoke-check step that probes a non-existent web `/health` endpoint.

For v1.0 specifically, I would treat the CI smoke-check mismatch and operational healthchecks as near-term blockers, and then address dependency/version pinning and detector fairness/performance shortly after release if schedule is tight.

## Release Blockers

| Finding | Why it blocks v1.0 | Evidence | Suggested fix |
|---|---|---|---|
| Compose smoke test checks `/health`, but web app appears to have no `/health` route | CI reliability/release confidence risk: smoke test may be invalid/flaky or permanently failing | `.github/workflows/build.yml` checks `http://localhost:8080/health`; no `/health` route in web source | Add a minimal `/health` endpoint in web, or update smoke test to check an existing route. |
| Missing healthchecks for web/detector/notifier/caddy in Compose | Runtime failure detection/recovery is weak; only Redis health is monitored | `docker-compose.yml` defines healthcheck only for Redis | Add meaningful healthchecks for each service and use `depends_on.condition: service_healthy` where appropriate. |

## Agent 1: Security Audit
### Findings
1. **HIGH — Web container has Docker socket mounted (host-root equivalent blast radius).**
   - `/var/run/docker.sock` is mounted into `web`, and admin routes directly use Docker API for log streaming.
   - This is partially acknowledged in comments and gated by app auth, but still materially increases impact of any future web compromise.
   - Evidence: `docker-compose.yml` volume mount; admin route notes and Docker client usage.

2. **MEDIUM — All service containers run as root by default.**
   - Dockerfiles do not set a non-root `USER`.
   - For a LAN/hobby deployment this is common, but it worsens post-compromise impact.
   - Evidence: `services/web/Dockerfile`, `services/notifier/Dockerfile`, `services/detector/Dockerfile`, `services/detector/Dockerfile.x86`.

3. **MEDIUM — Optional auth disable can expose full UI/API and admin features.**
   - If `system.auth.enabled=false`, middleware allows all requests through.
   - This can be intentional for trusted LANs, but should be clearly labeled “unsafe outside isolated LAN”.
   - Evidence: `services/web/src/main.py` auth middleware branch.

4. **LOW — Model upload validates extension and optional size, but not file content/signature.**
   - Path traversal is mitigated via basename usage and writes are atomic; this is good.
   - Remaining risk: malformed or malicious model files can still be uploaded and later loaded.
   - Evidence: `services/web/src/routes/models.py`.

5. **LOW — Dependency supply chain is largely unpinned.**
   - Requirements use broad/unpinned deps; only a couple lower bounds/caps exist.
   - Increases drift and surprise breakage/CVE churn over time.
   - Evidence: all three service `requirements.txt` files.

### Good practices observed
- YAML parsing uses `yaml.safe_load` consistently in config readers/watchers.
- Model upload uses temporary files + `os.replace` and basename sanitization.
- CSRF middleware and secure cookie settings are present.

## Agent 2: Reliability & Error Handling
### Findings
1. **MEDIUM — Notifier shutdown can hang while blocked in pub/sub listen loop.**
   - Signal handler flips `shutdown_flag`, but `pubsub.listen()` is blocking and not explicitly interrupted on shutdown.
   - This can delay clean stop/restart.
   - Evidence: `services/notifier/src/main.py` subscribe loop and signal handling.

2. **MEDIUM — MetricsStore connection is never explicitly closed.**
   - `MetricsStore` opens a long-lived SQLite connection, but no close API is invoked during detector shutdown.
   - Usually harmless in container teardown, but not ideal for graceful resource cleanup.
   - Evidence: `services/detector/src/metrics_store.py`; detector shutdown path in `services/detector/src/main.py`.

3. **MEDIUM — Multi-camera fairness risk when cameras share a model lock.**
   - Camera threads are independent (good), but shared-model inference is serialized under one lock.
   - A busy/noisy camera can dominate lock acquisition and increase latency for others.
   - Evidence: per-camera loop in `main.py` + detector lock in `detector.py`.

4. **LOW — Redis publish failures in detector are logged but events are dropped.**
   - DB persistence still occurs, but pub/sub consumers may miss notifications during Redis disruptions.
   - Consider bounded in-memory/disk retry if delivery guarantees matter.
   - Evidence: `services/detector/src/publisher.py`.

### Good practices observed
- RTSP reconnect with exponential backoff and stop-event aware waiting is implemented cleanly.
- Config watcher keeps last good config on parse/callback failure (no crash on malformed reload).

## Agent 3: Performance & Resource Efficiency
### Findings
1. **MEDIUM — `frame_skip` still decodes every frame (CPU/GPU waste under high FPS streams).**
   - Loop always calls `stream.read()` and discards skipped frames afterward.
   - Could be improved with `grab()`/`retrieve()` pattern or stream-side FPS limiting.
   - Evidence: `services/detector/src/main.py` frame loop.

2. **MEDIUM — Missing indexes on `detection_events` query patterns used by UI filters.**
   - Web routes query by camera/class/date/feedback and sort by id desc; table creation/migrations add columns but no corresponding indexes.
   - Likely to degrade as event volume grows.
   - Evidence: `services/web/src/db.py` query patterns; `services/detector/src/events.py` schema/index creation.

3. **LOW — SSE uses one Redis pubsub connection per browser stream.**
   - Fine for small deployments; can grow linearly with tabs/clients.
   - Could later fan out from a single backend subscription if needed.
   - Evidence: `services/web/src/routes/feed.py` and `services/web/src/routes/events.py`.

### Good practices observed
- Snapshot retention cleaner exists and prunes files + DB references.
- Detector uses buffer size hints and reconnect logic to reduce stale-frame behavior.

## Agent 4: Code Quality & Maintainability
### Findings
1. **MEDIUM — Logging format is plain-text, not structured JSON as project guidance states.**
   - Detector, notifier, and web startup all use standard text format strings.
   - Makes machine parsing/correlation harder in production.
   - Evidence: `services/detector/src/main.py`, `services/notifier/src/main.py`, `services/web/src/start.py`.

2. **LOW — ConfigWatcher code is duplicated across detector and notifier.**
   - Functionally identical logic maintained in two places.
   - Small shared module would reduce drift risk.
   - Evidence: `services/detector/src/config_watcher.py`, `services/notifier/src/config_watcher.py`.

3. **LOW — Internal/private DB helper is consumed by routes.**
   - Events route calls `db._connect()` directly.
   - Prefer public helper to keep module boundaries cleaner.
   - Evidence: `services/web/src/routes/events.py`.

4. **Known bug check — Compose path hardcoding issue appears resolved.**
   - Current compose uses named volumes (`scarguard-config`, `scarguard-models`, `scarguard-data`) instead of hardcoded `./config`, `./models`, `./data` bind paths.
   - Evidence: `docker-compose.yml`.

### Good practices observed
- Type hints are broadly used.
- Pydantic models are used in config handling and shared event/data models.
- Inline comments explaining concurrency and backward compatibility are strong.

## Agent 5: Deployment & Operations Readiness
### Findings
1. **HIGH — Compose lacks healthchecks for most services.**
   - Only Redis has healthcheck; detector/web/notifier/caddy rely on process-start success.
   - Weakens observability and automated recovery behavior.
   - Evidence: `docker-compose.yml`.

2. **HIGH — CI compose smoke test appears to check non-existent `/health` endpoint.**
   - Build workflow probes `http://localhost:8080/health`; no such route is defined in web app.
   - This is an automation drift bug.
   - Evidence: `.github/workflows/build.yml`; web route inventory in `services/web/src`.

3. **MEDIUM — Docs drift in README image/version details.**
   - README lists x86 base as `pytorch/pytorch:2.5.1...` and Redis as `redis:alpine`, while code uses `2.6.0...` and `redis:7-alpine`.
   - Evidence: `README.md`, `services/detector/Dockerfile.x86`, `docker-compose.yml`.

4. **MEDIUM — Version pinning via mutable tags (images/actions) risks non-determinism.**
   - Base images and Trivy action use tags/`@master` rather than digests or fixed SHAs.
   - Reasonable for hobby projects, but not ideal for release reproducibility.
   - Evidence: Dockerfiles and `.github/workflows/build.yml`.

5. **LOW — setup.sh is interactive via `/dev/tty`, limiting unattended automation.**
   - Fine for first-run UX, but CI/headless provisioning requires wrappers or manual env prep.
   - Evidence: `setup.sh` prompt reads.

### Good practices observed
- Setup script is idempotent and handles platform detection + upgrade backfills.
- CI includes lint/type/tests + containerized test runs + Trivy scanning.

## Cross-Cutting Analysis
- **Security × Ops:** Docker socket mount is the highest blast-radius design choice; it should remain behind auth and ideally behind an explicit feature flag for installations that don’t need admin log streaming.
- **Reliability × Performance:** Shared-model lock and frame-skip decode behavior together can increase latency for one camera while burning cycles on another; this manifests as both fairness and efficiency debt.
- **Ops × Quality:** Documentation and CI workflow drift (`/health` mismatch, version text mismatch) increase release risk more than code defects do.
- **Positive cross-cutting:** Config hot-reload failure handling, retry queue design, and retention policies show mature operational thinking for a hobby project.

## Prioritized Action List
| Priority | Finding | Category | Agents | Suggested Fix | Effort |
|----------|---------|----------|--------|---------------|--------|
| P0 | CI smoke test checks missing `/health` endpoint | Deployment/CI | 5,4 | Add `/health` route or update check to existing endpoint | S |
| P0 | Missing healthchecks for web/detector/notifier/caddy | Reliability/Ops | 5,2 | Add service-specific healthchecks in compose | S |
| P1 | Docker socket mounted into web container | Security/Ops | 1,5 | Gate admin logs behind config flag; consider sidecar/log API alternative | M |
| P1 | Containers run as root | Security | 1 | Add non-root user in Dockerfiles where feasible | M |
| P1 | Shared detector lock fairness under multi-camera load | Reliability/Performance | 2,3 | Introduce fair scheduling or per-camera inference queues | M |
| P2 | Missing indexes on detection_events for UI queries | Performance | 3 | Add indexes on `(timestamp)`, `(camera_name,timestamp)`, `(class_name,timestamp)`, `(feedback)` as needed | S |
| P2 | Unpinned Python/base/action versions | Security/Ops | 1,5 | Lock critical deps and pin key workflow actions to SHA | M |
| P3 | Non-structured logging despite project guidance | Maintainability/Ops | 4,5 | Move to JSON formatter or dual-mode logging | M |
| P3 | Notifier shutdown may block in pubsub loop | Reliability | 2 | Explicitly close pubsub/client on SIGTERM path | S |
| P3 | ConfigWatcher duplication | Maintainability | 4 | Consolidate shared watcher module | S |

## Quick Wins
- Add a lightweight `/health` endpoint in `web` (or update CI probe) — immediate confidence gain.
- Add basic compose healthchecks for `web`/`notifier`/`detector`/`caddy`.
- Replace `aquasecurity/trivy-action@master` with a fixed release tag or SHA.
- Update README image/version strings to match current Dockerfiles and Compose.

## Post-v1.0 Tracking
- Evaluate replacing Docker socket log tail with safer logging architecture (sidecar/aggregated logs).
- Revisit inference scheduling model for better fairness at 3+ cameras.
- Consider dependency lock strategy (`pip-tools`, pinned constraints) once release cadence stabilizes.
- Consider optional SSE fan-out optimization if multi-user dashboard usage increases.
