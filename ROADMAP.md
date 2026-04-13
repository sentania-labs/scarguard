# ScarGuard — Roadmap

Active and planned features. Each item includes acceptance criteria. Completed features (1–17, 18–22, 23–27) are in [ROADMAP_ARCHIVE.md](ROADMAP_ARCHIVE.md).
---

## Deterrence — Physical Deterrence (Scar's Revenge)

New `deterrent` Docker Compose service for automated physical deterrence. Subscribes to `scarguard:detections` on Redis and triggers Tuya smart devices via the **Tuya Cloud API** (`tinytuya.Cloud`). Supports sprinklers, lights, sirens, and smart plugs — any device in the Tuya/Smart Life ecosystem. Randomized activation patterns (device selection, duration, delays) to prevent wildlife habituation.

- **v0.13.0 (MVP):** Any detection fires all enabled devices with randomized timing and global cooldown. Opt-in via `deterrent.enabled` in config.
- **v0.13.x:** Response profiles (species-based routing, time-of-day conditions, device-type filtering). Example: heron = all deterrents ("Global Thermonuclear War"), raccoon at night = lights + sound only.
- **Hardware:** Off-the-shelf Tuya/Smart Life smart devices — hose timer valves, smart plugs, lights, sirens. Battery-powered devices work via Cloud API (LAN control not viable due to deep sleep).
- **Config:** `deterrent` section in `scarguard.yml` — Tuya Cloud credentials, device definitions (device_id, type), randomization ranges, cooldown, battery alert thresholds.
- **Setup guide:** [TUYA_SETUP.md](TUYA_SETUP.md) — step-by-step instructions for creating a Tuya IoT Platform account and obtaining API credentials.
- **Battery monitoring:** Periodic polling via Tuya Cloud API with low-battery alerts through the existing notification system.
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

## v0.12.10 — CodeQL hardening (in progress)

Final 0.12.x patch. Clears the three open CodeQL findings deferred from
the v0.12.7 review (tracked in issue #95) so 0.13 starts with a clean
security baseline.

1. **GitHub Actions token scoping.** Top-level `permissions: contents: read`
   default added to all four workflows (`ci.yml`, `build.yml`, `release.yml`,
   `cleanup.yml`). Per-job blocks already declared the minimum extras
   (`packages: write` for image push, `contents: write` only on the
   benchmark + release-creation jobs); the new top-level default ensures
   any future job inherits read-only by default. Resolves CodeQL
   `actions/missing-workflow-permissions` (alerts #4–#11).
2. **TLS cert upload path containment.** `upload_tls_cert` in
   `services/web/src/routes/config.py` no longer routes the destination
   filename through a `(name, data)` tuple list. The two writes are
   inlined with literal `"cert.pem"` / `"key.pem"` filenames so neither
   humans nor CodeQL's taint tracker can confuse the literal name with
   the user-supplied PEM bytes. (An earlier attempt added an
   `is_relative_to` containment check but CodeQL still flagged the
   tuple-iteration sink because it could not prove the first element was
   always literal.) Resolves CodeQL `py/path-injection` (alerts #15, #16).
3. **Caddy `caddy fmt` cleanup (re-fix).** v0.12.8 tried to clear the
   "Caddyfile input is not formatted" warning by editing
   `config/Caddyfile.template`, which is reference-only. The active
   Caddyfile is generated by a Python heredoc in
   `config/caddy-entrypoint.sh`, so the warning persisted on the Orin
   even after v0.12.9. Re-applied the fmt fix (tab indentation) to the
   real heredoc.
4. **Exception message scrubbing.** `save_structured_config` and the raw
   YAML `save_config` handler no longer return `str(exc)` directly. Both
   now log the full exception server-side under a short `request_id` and
   return a generic message + that ID to the client; `static/config.js`
   surfaces the request_id in the error banner so operators can grep the
   web logs. Resolves CodeQL `py/stack-trace-exposure` (alerts #13, #14).

## v0.12.8 — post-0.12.7 hardening & UX (released)

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

## v0.13 — deterrent service

The first version in which ScarGuard closes the detect → notify → *deter*
loop end-to-end. Introduces a new `deterrent` service that controls Tuya
smart devices (sprinklers, lights, sirens, plugs) via the Tuya Cloud API.
See `ACTUATION_SPEC.md` for the design and `TUYA_SETUP.md` for user-facing
setup instructions. Ships as **beta/opt-in** in 0.13.0 (MVP: all devices
fire on any detection with randomization + cooldown). 0.13.x patches add
response profiles (species → device-type routing, time-of-day conditions).
**1.0.0** is reserved for "deterrent validated in production" — i.e. the
first version where ScarGuard fully delivers on its name.

### v0.13.1 — deterrent web UI & security fixes (in progress)

1. **Actuation log page.** New `/admin/actuations` page showing every deterrent
   firing: timestamp, trigger detection (class + camera + confidence), devices
   fired with per-device success/failure, total duration. SSE live updates,
   filtering by trigger class/camera/date, pagination. Deterrent service
   persists events to its own SQLite DB (`/data/deterrent.db`); web reads
   read-only.
2. **Device status panel.** "Check Status" button on the deterrent page queries
   all devices via Tuya Cloud API (through the deterrent service over Redis
   request/response). Shows online/offline, battery %, and switch state.
3. **Per-device test-fire.** "Fire" button in the devices table activates a
   single device for 3 seconds. Uses the same Redis request/response pattern.
4. **Actuation defaults & battery monitor UI.** Cooldown, randomization ranges,
   battery monitor settings now configurable on the deterrent page.
5. **Detection publish fix.** Detector now publishes all detections to Redis
   regardless of action rules. Notifier already suppresses when no rule
   matches; deterrent sees everything.
6. **Security fixes.** XSS in event stream rendering, login redirect
   sanitization, explicit auth guard on `/snapshot/send`.
7. **About page.** Deterrent service status indicator.
8. **Log streaming.** Deterrent service added to log viewer filter.

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
- Tuya LAN fallback — for always-on (mains-powered) Tuya devices, offer `tinytuya` local TCP control as a lower-latency alternative to Cloud API. Not viable for battery-powered devices (WiFi radio sleeps between cloud check-ins).
- Config secrets at rest — encrypt sensitive values (API keys, SMTP passwords, webhook tokens) in `scarguard.yml`. Target v0.14.x or v0.15.x.
