# ScarGuard — Roadmap

Active and planned features. Each item includes acceptance criteria. Completed features (1–17, 18–22, 23–27) are in [ROADMAP_ARCHIVE.md](ROADMAP_ARCHIVE.md).
---

## Actuation — Sprinkler Deterrence (Scar's Revenge)

New `actuator` Docker Compose service for automated physical deterrence. Subscribes to `scarguard:detections` on Redis and triggers Tuya WiFi hose timer valves via `tinytuya` LAN control (no cloud dependency). Randomized spray patterns (valve count, duration, inter-spray delays) to prevent wildlife habituation.

- **Hardware:** Off-the-shelf Tuya/Smart Life WiFi hose timer valves, standard garden hose fittings, battery-powered. No custom wiring or relay boards.
- **Config:** `actuation` section in `scarguard.yml` — valve definitions (device_id, local_key, IP), randomization ranges, cooldown, battery alert thresholds.
- **Battery monitoring:** Periodic polling via tinytuya with low-battery alerts through the existing notification system.
- **Blocked on:** PoC valve hardware arrival and LAN control validation.
- **Full specification:** [ACTUATION_SPEC.md](ACTUATION_SPEC.md)

---

## Cleanup / Deprecation

Stale config keys are handled by a declarative `_STALE_KEYS` set in `config_store.py`. When removing a deprecated feature, add its YAML keys to the set — they'll be stripped on the next user-initiated save. No per-feature migration functions, no removal tickets for cleanup code.

- ~~**Remove legacy SSL→TLS migration** (target x.12.x)~~ — ✓ Removed in v0.12. Add `"ssl"` to `_STALE_KEYS`.
- **Remove legacy flat notification keys** (target x.13.x) — `notifications.discord` and `notifications.email` flat config keys. Deprecated in v0.10 with log warnings. Users should migrate to `notifications.channels` named channel format. Code to remove: fallback branch in `build_notifiers()` (notifier `main.py`), `DiscordConfig`/`EmailConfig` models in web `config_model.py`, legacy form sections in `config.html`, legacy write in `routes/config.py`. Add `notifications.discord` and `notifications.email` to stale-key stripping in `save()`.
- **Remove retention_days migration code** (target x.14.x) — auto-rewrite of `snapshot_retention_days`/`metrics_retention_days` → `retention_days` added in v0.11. Safe to remove once enough releases have passed. Add `"snapshot_retention_days"` and `"metrics_retention_days"` to `_STALE_KEYS`.

---

## ~~Hardening (0.12.x cycle)~~ — ✓ Complete (v0.12.3–v0.12.6)

All 13 items from the beta 1 code audit shipped across four patch releases. One item (structured JSON logging) was intentionally dropped — reduces readability for self-hosters who tail logs directly. See git history for v0.12.3–v0.12.6 for details.

## v0.12.7 — bundled release (released)

Three workstreams bundled into one patch release:

1. **Inference perf hotfix.** Pins ultralytics' predict save_dir so the `increment_path` O(N) scan doesn't dominate inference time. Regression introduced in v0.12.1 via commit `149374d` (the non-root container fix for #77) went undetected for six patch releases. Recovered v0.11.0 performance (~50 ms per call) on the production Orin via a one-line change in `detector.py`. See `INFERENCE_INVESTIGATION.md` for the full post-mortem including ~4 hours of wrong theories chased before py-spy gave the answer in 30 seconds.
2. **Viewer role + sensitive-field redaction.** New third auth tier between `user` and `admin`: can view everything including admin pages but with every plaintext secret masked as `***REDACTED***`, zero write access, raw-YAML tab hidden entirely. Enables oversight-without-risk for family members and sysadmins. First server-side redaction helper in the codebase, closing a pre-existing authz gap where several admin routes (config, training, logs) had no role gating at all.
3. **Last-admin protection.** Lockout-prevention check on the user-management routes — cannot delete, disable, or demote the last active admin.

## v0.12.8 — post-0.12.7 hardening & UX (in progress)

Final 0.12.x patch before the 0.13 actuator service. Scope is limited to
items surfaced by ~24h of 0.12.7 running in production plus three small UX
requests. Establishes a clean, fully-healthy baseline and a baseline audit
trail before the big 0.13 feature lands.

1. **Log-streamer healthcheck fix.** The v0.12.6 YAML folded-scalar `python -c`
   produced an `IndentationError: unexpected indent` on every healthcheck
   probe, so the container reported `unhealthy` for 23h+ despite actually
   running fine. Replaced with exec-form JSON array (`["CMD", "python3", "-c", "..."]`)
   which bypasses the YAML folding issue entirely.
2. **Caddy edge deny for bot probes.** Added a `@probes` matcher in the
   Caddyfile template that returns 404 for common scanner paths
   (`/.git/*`, `/_ignition/*`, `/aws*config.js`, `/config.js`) before they
   reach FastAPI. 404 (not 403) is intentional — quieter, fewer follow-ups.
3. **`caddy fmt` cleanup.** Normalised the Caddyfile template to tabs so
   Caddy stops logging the "input is not formatted" warning on every reload.
4. **Feedback-by-link resubmit.** The token route used to hard-block any
   POST once `feedback` was set, making it impossible to correct a wrong
   first choice. Block removed (token 7-day expiry remains the real
   single-use protection); template gained a corrected-class picker that
   actually lets users record which class is correct.
5. **Snapshot overlay auto-close.** Submitting feedback from the events
   page snapshot overlay now closes the overlay instead of leaving it
   open.
6. **Minimum-viable audit log.** New `audit_events` table in auth.db +
   `/admin/audit-log` admin-only viewer. Records login success/failure,
   logout, config saves, user create/delete/role-change/password-reset/
   disable/enable, and api_token create/revoke. Intentionally scoped small:
   retention policies, CSV export, structured config diffs, RBAC on the
   viewer, and hooks on backup/arm/model/TLS routes are deferred to 0.13.x.

---

## v0.13 — actuator service (planned)

The first version in which ScarGuard closes the detect → notify → *deter*
loop end-to-end. Introduces a new `actuator` service that controls a Tuya
WiFi valve for sprinkler-based heron deterrence. See
`ACTUATION_SPEC.md` for the design. Expected to ship as a **beta/opt-in**
in 0.13.0, with 0.13.x patches hardening based on real-world use. **1.0.0**
is reserved for "actuator validated in production" — i.e. the first
version where ScarGuard fully delivers on its name.

---

## Future Ideas (Unprioritized)

- Twilio SMS notifications — paid per-message, but works on any phone without an app
- Per-class cooldown — different cooldown values per detected species (e.g. 30s for squirrels, 5min for herons)
- Mobile-friendly layout — basic nav responsiveness added in v0.11 (admin menu touch support, nav wrapping); full responsive CSS for all pages remains a future item
- UI polish — logo, favicon, login screen branding. Japanese-inspired koi aesthetic. Use AI image generation for assets. Lowest priority.
- ONVIF camera auto-discovery — scan local network for RTSP cameras instead of manual URL entry. Helps non-UniFi users.
- Home Assistant MQTT discovery — auto-register ScarGuard as an HA device, beyond generic webhook
- Confidence auto-tuning — analyze feedback data to suggest optimal confidence thresholds per class
- Night/IR mode awareness — detect when cameras switch to IR and adjust confidence thresholds or flag detections accordingly
- NVR-lite — proxy live RTSP video + audio through the web UI (HLS or WebRTC). Significant scope; dedicated NVR tools (Frigate, Protect) already do this well.
- S3/Minio remote config backup — upload config backups to object storage for off-device redundancy
- Automated Orin runner/self-updates — CI pushes runner updates to Orin via SSH (parked)
- Isolated benchmark runner — use concurrency groups or dedicated runner labels at release time to ensure CPU inference benchmarks run without competing workloads skewing FPS numbers
- CI path filtering — skip full CI on docs-only or benchmark-only PRs using `paths-ignore` in workflow triggers. Needs a lightweight "skip" job if CI becomes a required status check.
