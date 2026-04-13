# ScarGuard Pre-Release Code Review
**Date:** 2026-04-13
**Commit:** a3b17a1
**Reviewed by:** Claude Code (5-agent audit)

## Executive Summary
This audit reviewed the v0.13.1 codebase across security, reliability, performance, maintainability, and deployment operations. Overall, ScarGuard is in good shape for a hobby/home deployment: key hardening choices are visible (non-root containers for core services, `yaml.safe_load`, auth gating on sensitive routes, atomic config writes in web, RTSP reconnect backoff, FairLock for camera fairness, and meaningful health checks).

The main release risk is **operational/documentation drift** and a few **security/ops gaps** that are acceptable for LAN-only use but become risky if exposed publicly. The two strongest examples are: (1) Redis auth can still be disabled by empty `REDIS_PASSWORD`, and (2) the new `log-streamer` sidecar still requires Docker socket access (host-equivalent risk if compromised). These are not necessarily blockers for a private LAN install, but they should be called out clearly and tightened with low complexity.

From a runtime behavior perspective, detector/notifier resilience is generally solid (reconnect loops, retry queue, graceful shutdown hooks). Remaining concerns are mostly edge-case robustness (e.g., one non-atomic config write path in detector scheduler), plus scalability limits in SSE/log streaming if many clients/tabs are opened.

## Release Blockers
| Finding | Why it blocks/should block | Evidence |
|---|---|---|
| None strictly required for a LAN-only v1.0. | No critical RCE/data-corruption issue found in default compose deployment. Highest risks are contextual (public exposure, docker.sock sidecar compromise). | `docker-compose.yml` has no direct web port mapping except via Caddy and uses service-level auth patterns. (`docker-compose.yml:93-123`, `services/web/src/main.py:167-255`) |

## Agent 1: Security Audit
### Findings
1. **MEDIUM — Redis auth is optional and defaults empty.**  
   - `redis` runs without `--requirepass` if `REDIS_PASSWORD` is empty, and `.env.example` ships empty.  
   - Risk is moderate on default isolated compose network, but higher if someone later exposes Redis, joins shared Docker networks, or misconfigures host firewall.  
   - Evidence: `docker-compose.yml:20-34`, `.env.example:28-31`.

2. **HIGH — Docker socket is exposed to log-streamer (host-equivalent capability).**  
   - `/var/run/docker.sock` is mounted into `log-streamer`; compromise of this container is effectively host-level control in many Docker setups.  
   - This may be an acceptable tradeoff for admin logs in a hobby deployment, but should be documented as high impact and constrained where possible.  
   - Evidence: `docker-compose.yml:180-204`.

3. **LOW (positive) — model upload path traversal protections are correct.**  
   - Upload destination uses `Path(filename).name`, extension allowlist, chunked write, and optional size cap.  
   - This is a strong implementation for this scope.  
   - Evidence: `services/web/src/routes/models.py:53-107`.

4. **LOW (positive) — config/YAML parsing uses safe loaders.**  
   - Observed `yaml.safe_load` in detector, notifier, config watcher, and web routes.  
   - Evidence: `services/detector/src/main.py:59-61`, `services/notifier/src/main.py:50-52`, `shared/config_watcher.py:64-66`, `services/web/src/routes/config.py:228-233`.

5. **LOW — some dependencies are not strictly pinned.**  
   - `tinytuya>=1.16.1` and several `tzdata` entries are unpinned.  
   - For hobby project this is acceptable, but pinning improves reproducibility and supply-chain stability near v1.0.  
   - Evidence: `services/deterrent/requirements.txt:1-5`, `services/web/requirements.txt:1-11`, `services/notifier/requirements.txt:1-8`.

## Agent 2: Reliability & Error Handling
### Findings
1. **LOW (positive) — RTSP reconnect behavior is robust and explicit.**  
   - Stream class includes reconnect backoff with max delay and socket timeout guard, plus frame-grab fallback for skipped frames.  
   - Evidence: `services/detector/src/stream.py:13-138`, `services/detector/src/main.py:253-271`.

2. **MEDIUM — detector scheduler writes config non-atomically in one path.**  
   - `_write_armed_to_config` uses plain open/write YAML in detector, not the atomic temp-file + replace strategy used by web `config_store.save()`.  
   - Risk: truncated/partial config if interrupted mid-write (rare but avoidable).  
   - Evidence: `services/detector/src/main.py:391-404`, contrast with `services/web/src/config_store.py:65-83`.

3. **LOW (positive) — notifier Redis reconnect and retry queue behavior is good.**  
   - Exponential reconnect for Redis, retry queue with persistence/backoff/max-age, and pubsub cleanup in `finally`.  
   - Evidence: `services/notifier/src/main.py:212-301`, `services/notifier/src/notification_queue.py:1-222`.

4. **LOW — broad `except Exception` usage is common but mostly logged.**  
   - There are several broad handlers, but most log exception context and continue safely.  
   - This is acceptable for long-running services; just ensure critical failures remain visible.  
   - Evidence examples: `services/notifier/src/main.py:107-109,202-203`, `shared/config_watcher.py:72-73`, `services/detector/src/events.py:135-140`.

5. **LOW — graceful shutdown mostly handled; daemon threads could hide linger bugs.**  
   - Signal handlers and stop events are present, and key resources are closed. Some threads are daemonized, so abrupt process exit may skip full cleanup in pathological shutdowns.  
   - Evidence: `services/detector/src/main.py:433-441,738-751`, `services/notifier/src/main.py:330-339`.

## Agent 3: Performance & Resource Efficiency
### Findings
1. **LOW (positive) — frame-skip path uses `grab()` not `read()`.**  
   - This avoids unnecessary decode for skipped frames and addresses a common RTSP waste pattern.  
   - Evidence: `services/detector/src/main.py:255-263`, `services/detector/src/stream.py:117-136`.

2. **LOW (positive) — fairness fix is in place for multi-camera shared model.**  
   - `FairLock` ensures FIFO model access across camera threads; likely resolves prior starvation issue materially.  
   - Evidence: `services/detector/src/fair_lock.py:1-65`, `services/detector/src/detector.py:24-83`.

3. **MEDIUM — SSE/log streaming scales linearly per client and can become expensive with many tabs.**  
   - Each client opens its own Redis pubsub connection and loop for events/logs; no multiplex layer. Fine for home use, but can load Redis/web when many sessions are open.  
   - Evidence: `services/web/src/routes/events.py:179-205`, `services/web/src/routes/admin.py:60-103`.

4. **LOW (positive) — SQLite indexes for core UI query patterns are present.**  
   - Timestamp/camera/class/feedback indexes and token unique index are explicitly created.  
   - Evidence: `services/detector/src/events.py:182-217,254-260`.

5. **LOW — image sizes and apt upgrade choices increase build/runtime footprint.**  
   - No multi-stage builds for Python services (reasonable for this repo), and `apt-get upgrade -y` in runtime images can increase build time/size variance.  
   - Evidence: `services/web/Dockerfile:1-14`, `services/notifier/Dockerfile:1-14`, `services/log-streamer/Dockerfile:7-15`.

## Agent 4: Code Quality & Maintainability
### Findings
1. **LOW — type hints are generally good in core paths.**  
   - Detector/notifier core loops and shared components are heavily typed.  
   - Evidence: `services/detector/src/main.py`, `services/notifier/src/main.py`, `shared/models.py`.

2. **MEDIUM — some direct dict-based config handling bypasses stronger schemas in non-web services.**  
   - Web uses Pydantic models (`StructuredConfigPayload`), but detector/notifier still rely heavily on untyped dict access, increasing mismatch risk on future config changes.  
   - Evidence: `services/detector/src/main.py:345-358,542-607`, `services/notifier/src/main.py:304-357`, `services/web/src/routes/config.py:54-94`.

3. **LOW — known hardcoded path issue appears resolved in compose.**  
   - `docker-compose.yml` now uses named volumes (`scarguard-config`, `scarguard-models`, `scarguard-data`) instead of hardcoded `./config`, `./models`, `./data`.  
   - Evidence: `docker-compose.yml:84-119,209-221`.

4. **MEDIUM — docs/code mismatch around Docker socket location.**  
   - `INFRASTRUCTURE.md` still states socket mount is in web container, but compose moved it to log-streamer. This can mislead security reviews and operators.  
   - Evidence: `INFRASTRUCTURE.md:130-136` vs `docker-compose.yml:180-204`.

## Agent 5: Deployment & Operations Readiness
### Findings
1. **LOW (positive) — first-run setup is practical and upgrade-aware.**  
   - `setup.sh` handles platform detection, Docker checks, runtime checks, `.env` generation/backfill, and config volume initialization.  
   - Evidence: `setup.sh:1-260`.

2. **MEDIUM — version pinning strategy is mixed (good in Docker base digests, weaker in service deps/tags).**  
   - Many Docker bases are digest-pinned (good), but runtime redis image is tag-only (`redis:7-alpine`) and some Python deps are range/unpinned.  
   - Evidence: `docker-compose.yml:18`, `services/*/Dockerfile`, `services/deterrent/requirements.txt:1-5`.

3. **LOW — health checks exist for all major services and are meaningful enough for hobby scale.**  
   - Web uses HTTP `/health`, worker-style services use heartbeat file/Redis ping.  
   - Evidence: `docker-compose.yml:78-83,110-115,138-143,166-171,194-201`.

4. **LOW — restart policy is consistent but has no crash-loop backoff/circuit breaker.**  
   - `restart: unless-stopped` on all services; acceptable for this deployment model, but repeated crash loops can spam logs and hide root cause.  
   - Evidence: `docker-compose.yml:19,62,91,123,152,178,207`.

5. **MEDIUM — upgrade path/migration signaling could be clearer in docs.**  
   - Code includes migrations (`retention_days`, `role`, `base_url`), but operator docs could more explicitly state what happens for old installs and what requires manual intervention.  
   - Evidence: `services/web/src/main.py:61-163`, `services/web/src/auth.py:_migrate_add_role_column`, `ROADMAP.md` release notes.

## Cross-Cutting Analysis
### Deduplicated cross-cutting concerns
1. **Docker socket risk + doc drift** (Security + Ops + Maintainability)  
   - Technical risk exists by design (`log-streamer` needs docker.sock), but documentation currently points to the wrong container, making risk review harder and incident response slower.

2. **Config robustness inconsistency** (Reliability + Maintainability)  
   - Web has atomic config persistence, but detector scheduler writes config via non-atomic open/write. Same asset (`scarguard.yml`) has mixed write safety guarantees.

3. **Pinning/reproducibility gap** (Security + Ops)  
   - Container base image pinning is solid in most places, but redis tag and some Python deps remain mutable across time.

### What is done well (cross-cutting positives)
- RTSP and Redis resilience patterns are thoughtfully implemented.
- Security-hardening work from earlier cycles is visible (safe YAML, route auth guards, redaction support, non-root user in key services).
- Detector fairness/perf improvements (FairLock + `grab()` frame skip) are practical and low-complexity wins.

## Prioritized Action List
| Priority | Finding | Category | Agents | Suggested Fix | Effort |
|----------|---------|----------|--------|---------------|--------|
| P1 | Docker socket exposure in `log-streamer` | Security/Ops | 1,5,Synthesis | Keep as-is for v1.0 if needed, but document risk prominently and constrain service (read-only FS, drop caps, seccomp, minimal network). | M |
| P1 | Redis auth can be disabled by empty password | Security/Ops | 1,5,Synthesis | Make non-empty `REDIS_PASSWORD` mandatory in production path; fail fast in setup/compose when blank (allow explicit dev override flag). | S |
| P2 | Non-atomic config write path in detector scheduler | Reliability | 2,Synthesis | Reuse shared atomic save helper or web `config_store` strategy for scheduler writes. | S |
| P2 | INFRASTRUCTURE doc mismatch (docker.sock on web vs log-streamer) | Maintainability/Ops | 4,5,Synthesis | Update INFRASTRUCTURE.md and README log architecture notes. | S |
| P2 | Mixed dependency/version pinning (`tinytuya`, `tzdata`, redis tag) | Security/Ops | 1,5,Synthesis | Pin versions for release branch or define explicit update cadence with CI safety checks. | S |
| P3 | SSE/log streaming linear-per-client scaling | Performance/Reliability | 3,Synthesis | For now: enforce sane client-side reconnect/backoff + optional server-side per-IP/tab limits; revisit fanout design post-v1.0. | M |
| P3 | Dict-heavy config access outside web | Maintainability | 4,Synthesis | Introduce incremental typed config adapters in detector/notifier for high-risk sections only. | M |

## Quick Wins
- Update docs to reflect current log-streamer/docker.sock architecture (high clarity gain, tiny effort).
- Require generated non-empty Redis password by default in setup and emit strong warning/error if blank.
- Align remaining unpinned deps (`tinytuya`, `tzdata`) and `redis:7-alpine` policy before tagging.
- Reuse atomic write helper for detector scheduler config mutation.

## Post-v1.0 Tracking
- Optional: add lightweight hardening to `log-streamer` container (cap drop, readonly rootfs, no-new-privileges).
- Optional: SSE fanout optimization if household usage grows (many concurrent clients/tabs).
- Optional: progressively type config handling in detector/notifier to reduce future schema-drift bugs.
