# ScarGuard — Roadmap

Active and planned features. Each item includes acceptance criteria. Completed features (1–27) are in [ROADMAP_ARCHIVE.md](ROADMAP_ARCHIVE.md).
---

## Deterrence — Physical Deterrence (Scar's Revenge)

New `deterrent` Docker Compose service for automated physical deterrence. Subscribes to `scarguard:detections` on Redis and triggers Tuya smart devices via the **Tuya Cloud API** (`tinytuya.Cloud`). Supports sprinklers, lights, sirens, and smart plugs — any device in the Tuya/Smart Life ecosystem. Randomized activation patterns (device selection, duration, delays) to prevent wildlife habituation.

- **v0.13.0 (MVP):** Any detection fires all enabled devices with randomized timing and global cooldown. Opt-in via `deterrent.enabled` in config.
- ~~**v0.13.x:** Response profiles (species-based routing, time-of-day conditions, device-type filtering).~~ — ✓ Species-based routing shipped in v0.13.3 via deterrent groups + per-camera `deterrent_rules`. Time-of-day conditions remain a Future Idea.
- **Hardware:** Off-the-shelf Tuya/Smart Life smart devices — hose timer valves, smart plugs, lights, sirens. Battery-powered devices work via Cloud API (LAN control not viable due to deep sleep).
- **Config:** `deterrent` section in `scarguard.yml` — Tuya Cloud credentials, device definitions (device_id, type), randomization ranges, cooldown, battery alert thresholds.
- **Setup guide:** [TUYA_SETUP.md](TUYA_SETUP.md) — step-by-step instructions for creating a Tuya IoT Platform account and obtaining API credentials.
- **Battery monitoring:** Periodic polling via Tuya Cloud API with low-battery alerts through the existing notification system.
- **Full specification:** [ACTUATION_SPEC.md](ACTUATION_SPEC.md)

---

## Cleanup / Deprecation

Stale config keys are handled by a declarative `_STALE_KEYS` set in `config_store.py`. When removing a deprecated feature, add its YAML keys to the set — they'll be stripped on the next user-initiated save. No per-feature migration functions, no removal tickets for cleanup code.

- ~~**Remove legacy SSL→TLS migration** (target x.12.x)~~ — ✓ Removed in v0.12. Add `"ssl"` to `_STALE_KEYS`.
- ~~**Remove legacy flat notification keys** (target x.13.x)~~ — ✓ Removed in v0.13.2. Add `"notifications.discord"` and `"notifications.email"` to stale-key stripping in `save()`.
- ~~**Remove retention_days migration code** (target x.14.x)~~ — shipping in v1.14.0. Adds `"snapshot_retention_days"` and `"metrics_retention_days"` to `_STALE_KEYS`; they'll be stripped on the next user-initiated save.
- **Remove secrets-at-rest plaintext passthrough** (target v1.15.x) — v1.14.0 introduced envelope encryption for sensitive YAML fields with a plaintext-passthrough branch in `shared/secret_box.decrypt` for one-time migration. Remove in v1.15 so unencrypted values are rejected at load time.
- **Remove config schema version sentinel for v1.14 migration** (target v1.15.x) — the one-shot `"upgrade in progress"` warning for plaintext secrets can come out once every active deployment has done one save.

---

## ~~Hardening (0.12.x cycle)~~ — ✓ Complete (v0.12.3–v0.12.6)

All 13 items from the beta 1 code audit shipped across four patch releases. One item (structured JSON logging) was intentionally dropped — reduces readability for self-hosters who tail logs directly. See git history for v0.12.3–v0.12.6 for details.

## v0.12.7 — bundled release (released)

Three workstreams bundled into one patch release:

1. **Inference perf hotfix.** Pins ultralytics' predict save_dir so the `increment_path` O(N) scan doesn't dominate inference time. Regression introduced in v0.12.1 via commit `149374d` (the non-root container fix for #77) went undetected for six patch releases. Recovered v0.11.0 performance (~50 ms per call) on the production Orin via a one-line change in `detector.py`. See `INFERENCE_INVESTIGATION.md` for the full post-mortem including ~4 hours of wrong theories chased before py-spy gave the answer in 30 seconds.
2. **Viewer role + sensitive-field redaction.** New third auth tier between `user` and `admin`: can view everything including admin pages but with every plaintext secret masked as `***REDACTED***`, zero write access, raw-YAML tab hidden entirely. Enables oversight-without-risk for family members and sysadmins. First server-side redaction helper in the codebase, closing a pre-existing authz gap where several admin routes (config, training, logs) had no role gating at all.
3. **Last-admin protection.** Lockout-prevention check on the user-management routes — cannot delete, disable, or demote the last active admin.

## v0.12.10 — CodeQL hardening (released)

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

### v0.13.1 — deterrent web UI & security fixes (released)

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

### v0.13.4 — chip autocomplete + model class introspection (released)

Closed-world tokens everywhere + silent-save hotfix + banner UX.

1. **Shared `chip-picker.js` component** — type-ahead chip input replacing
   the comma-separated text fields for (1) camera `notification_rules[].channels`,
   (2) camera `deterrent_rules[].groups`, (3) `summary_report.channels`,
   (4) global `detection.target_classes`, (5) per-camera `detect_classes`,
   (6) rule `class_name` (single-chip variant with `*` always-available).
   Unknown chips render with a warning color so typo'd references are
   visible instead of silent.
2. **Model class introspection** — detector exposes `model.names` via a
   Redis-RPC handler (`scarguard:model.classes.request`).  Web service
   proxies through a new `/models/{filename}/classes` endpoint, cached by
   `(path, mtime)` on both sides.  The `/models` admin page grew a
   Classes column with an expandable chip cloud + copy-to-clipboard.
   TensorRT `.engine` files without embedded names return an empty list
   + a warning pointing back at the source `.pt`.
3. **Soft-warn orphan references** — server-side check on both
   `POST /config` (raw YAML) and `POST /config/structured`.  Save still
   succeeds; response includes a `warnings` list for any rule or
   `summary_report.channels` entry that doesn't resolve to a defined
   channel or group.  Reminds the user until they fix it.
4. **Banner scroll-into-view** — save feedback scrolls into view so the
   top-of-form banner is visible regardless of which sub-tab or scroll
   position the user was editing.
5. **Hotfix: silent save** — stale `data.notifications.email` reference
   in `validate()` (left over from v0.13.2's flat-key removal) threw
   `TypeError` synchronously in `saveConfig()` before the try/catch was
   set up.  UI-based config saves silently dropped — no banner, no POST,
   no server logs.  Removed the dead validation block.
6. **Authenticated Docker Hub pulls** — v0.13.3 switched image bases to
   `mirror.gcr.io/library/*` as a workaround for Docker Hub's anonymous
   IP-pool rate limit (~100/6h) hitting GHA shared runners.  Replaced
   with `docker/login-action@v3` in build.yml using new org secrets
   `DOCKER_HUB_USERNAME` + `DOCKER_HUB_API_KEY`, and flipped Dockerfile
   `FROM` lines back to `docker.io/library/*`.  Authenticated pulls get
   the per-user quota (5000/day), ending our dependency on Google's
   Docker Hub mirror staying public.

### v0.13.2 — review fixes + deprecation removal (released)

Bundled post-v0.13.1 patch:

1. **Atomic config write** added to the detector scheduler for the arm/disarm
   transition writer (matches the pattern already used by `config_store.save()`).
2. **Deps pinned** — `tinytuya`, `tzdata`, and the Redis image pinned to a
   SHA256 digest.
3. **Legacy flat notification keys removed.** `notifications.discord` and
   `notifications.email` are no longer read by the notifier, and
   `config_store.save()` strips them via `_STALE_NOTIFICATION_KEYS` on the
   next user-initiated save. Named channels under `notifications.channels`
   are the only supported format.
4. **Documentation cleanup** — README, CONFIG_REFERENCE, and ROADMAP
   updated to reflect the deprecation removal.
5. **Redis auth guidance** added to `.env.example` — `setup.sh` now
   auto-generates a strong random `REDIS_PASSWORD` on first run.

---

## v1.14.0 — GA / beta-3 hardening (in progress)

The GA cut. ScarGuard's detect → notify → *deter* loop has been running
against real herons since v0.13.0 and hardened across v0.13.1–v0.13.5, so
the `1.0.0`-reserved "deterrent validated in production" bar is met.
Version number skips straight to v1.14.0 — GA jump out of 0.x while
preserving the `.14` minor sequence so existing roadmap cleanup items
align.

Scope is driven by two independent external code reviews (Claude + Codex,
2026-04-22) captured in `scarguard-review.md`. Everything Critical + High +
Medium + applicable Low ships here, plus roadmap-promised cleanup.

Workstreams (see `.claude/plans/` or the v1.14 PRs for full detail):

1. **Physical-safety hardening.** Duration caps + watchdog OFF +
   reconciliation loop + emergency-off endpoint/UI + audit `request_id` on
   every actuation. Closes review items C1, C2, Codex #1/M4/M5. Largest
   blast-radius reduction in the release.
2. **HMAC-signed detection bus.** `DETECTION_HMAC_KEY` generated by
   `setup.sh`; detector signs publishes; deterrent + notifier verify
   before acting. Closes H2.
3. **Envelope-encrypted secrets at rest.** Sensitive YAML fields
   (`tuya.access_id/secret`, channel `smtp_pass`, webhook `auth_token`,
   ntfy `token`) stored as `enc:v1:<fernet>`; key in `/data/secret_key`
   (chmod 600, generated by `setup.sh`). One-shot auto-migration on first
   save after upgrade. `scripts/rotate-secret-key.sh` for rotation.
   Closes C3, M13.
4. **Web hardening.** Bootstrap token required for `/setup` (closes
   Codex #2); webhook/Discord URL SSRF guard (closes H1); persistent CSRF
   secret (M5); CSP nonce migration + HSTS + Permissions-Policy (M6,
   Codex-M3); `forwarded_allow_ips` → CIDR (Codex-M2); YAML upload size
   cap (M2); password policy 12+ (M4).
5. **Redis-backed rate limiting.** Token-bucket helper + per-endpoint
   dependency; applied to physical-control routes (`/test-fire`, `/arm`,
   `/disarm`, `/force-off`), SSE streams, feedback, config POST, model
   upload. Closes Codex #3, M7.
6. **Ops / compose hardening.** Resource limits on every service
   (`mem_limit`, `cpus`, `pids_limit`); `cap_drop` + `no-new-privileges`
   + `read_only` where feasible; `tecnativa/docker-socket-proxy`
   replacing raw socket mount in log-streamer; SQLite backup sidecar
   with admin UI (list / manual-trigger / download); active periodic
   healthchecks (detector/notifier/deterrent); RTSP reconnect long-backoff
   cap; queue-overflow metric. Closes H4, H5, H6, M8, M11, Codex-M7/M8/M10.
7. **Code hygiene.** SQL fragment helper (M3); PRAGMA integrity_check on
   boot (M9); exception-logging hygiene + ruff `BLE001` (M10); token-hash
   audit (M14); cert/key repo audit (L4); feedback-token HMAC (L6);
   example-config cleanup (L2).
8. **Cleanup.** Adds `"snapshot_retention_days"` / `"metrics_retention_days"`
   to `_STALE_KEYS` (this roadmap's x.14 cleanup commitment).
9. **Documentation.** New `SECURITY.md`, `BACKUP.md`,
   `docs/EMERGENCY_OFF.md`; INFRASTRUCTURE.md additions for resource
   limits, backup architecture, secret-rotation playbook; README version
   table reconciled through v1.14.0; self-review rule moves from CLAUDE.md
   to CONTRIBUTING.md (D9); CONFIG_REFERENCE parity pass against
   `config_model.py` (D10).

### Deferred from v1.14.0

These items were considered and pushed out. Captured here so they aren't
silently dropped.

- **Web-write-config split into a dedicated `config-api` service** (Claude H3) —
  mitigates "FastAPI compromise = full-system compromise" cleanly but is a
  larger architectural change. v1.14's strict Pydantic validation + backup
  sidecar + read-only mounts on non-web services limit blast radius. Revisit
  if a future audit flags actual exploitation paths.
- **Tamper-evident actuation audit chain (signed hash-chain per record)**
  (Codex-M6) — `request_id` + standard audit table is the v1.14 level.
  Hash-chain in a later release if the threat model demands it.
- **Multi-stage Docker builds** (Claude L3) — image-size optimization, not
  a security or correctness issue.
- **Web service multi-replica support** — v1.14 adds the persistent CSRF
  secret scaffold (M5) but compose still assumes a single `web` replica.
  Real multi-replica (sticky sessions, shared session store) is future
  work, if ever.
- **zxcvbn password strength scoring** — v1.14 ships minimum-length (12) +
  top-1000-common-password rejection. Full entropy scoring is deferred;
  the marginal benefit on an admin-only surface is small.
- **pip-audit in CI** — Trivy already scans built images for Python CVEs
  at CRITICAL/HIGH. pip-audit would add coverage at the `requirements.txt`
  layer but is not a net-new signal. Revisit if Trivy's Python coverage
  weakens.
- **CSP nonce migration** — v1.14 keeps `'unsafe-inline'` in
  `script-src` because dropping it requires moving every inline `<script>`
  block in the Jinja templates to a per-request-nonce mechanism (or out
  to static .js files). Real defence against template-injection XSS
  needs this, but the scope is wide. Track for a follow-up release.
- **Vendor htmx + Chart.js** — v1.14 still loads
  `https://unpkg.com/htmx.org`, `htmx-ext-sse`, and `chart.js` from the
  CDN, which means CSP can't drop the unpkg.com host. Vendoring into
  `services/web/src/static/vendor/` adds ~300 KB to the image but
  removes the third-party CDN trust boundary.
- **SSE concurrent-connection caps** — the v1.14 rate limiter is a
  fixed-window request counter, which is the wrong primitive for SSE
  (one stream = one long-lived connection). Real SSE protection needs
  connection tracking (hold a Redis SET of active stream IDs per
  principal). Defer.
- **`scripts/rotate-secret-key.sh`** (Workstream C C5) — operator
  tooling for rotating `/data/secret_key` with re-encryption of every
  `enc:v1:` field. Plan called for it; deferred to keep C scoped.
  Manual procedure documented in SECURITY.md will work as a stopgap.
- **Test-fire actuation_db persistence** (Workstream A polish) —
  detection-driven actuations are persisted to the audit DB; admin-
  triggered test-fires are logged but not in the DB. For a fully
  symmetric audit trail, persist test-fires too. Logged at INFO with
  `[rid=...]` so the trail is recoverable from logs in the meantime.
- **Sticky banner for `scarguard:deterrent:stuck` events** (A polish) —
  events are published to Redis and logged at CRITICAL; the dashboard
  doesn't yet subscribe to surface them as a banner. Needs a web-side
  Redis subscriber + SSE push or HTMX refresh hook.
- **Standardised `shared/redis_client.py` retry helper** (Workstream F F7) —
  every service has its own ad-hoc Redis client with reconnect backoff.
  The plan called for unifying these behind a single helper, but
  existing per-service code already retries with backoff; deferred to
  avoid touching every service's main loop just for tidiness.
- **Live status SSE for `/admin/db-backups`** — the page currently
  reloads after a manual trigger to pick up the new file. A proper
  SSE feed against `scarguard:backup:status` would surface progress
  live (started → in-progress → completed) without the reload. Polish.
- **setup.sh starter-model end-to-end verification** (Workstream I I6) —
  the README claims setup.sh prompts to download a starter YOLO model
  for testing the pipeline. v1.14 did not re-verify this path end-to-
  end. Worth a manual run on a fresh host before the v1.14.0 tag.
- **TensorRT export verification** (Workstream I I7) — README's
  `docker compose exec detector python -c "..."` TensorRT export
  command wasn't re-verified post-non-root-container-fix. If it fails
  as the `scarguard` user, document `-u root` or fix the write perms
  on `/models`.
- **CI sweep of Bearer-auth endpoints** (Workstream I I8) — the README
  lists endpoints as Bearer-accessible. A CI test that enumerates each
  and asserts non-401 with a valid token would catch doc/code drift.
  Net-new CI; defer.
- **CONFIG_REFERENCE.md ↔ config_model.py full parity pass**
  (Workstream I I10) — v1.14 added `backup:`, `reconcile_interval_sec`,
  and the new encrypted-field paths. Spot-check suggests the file is
  mostly in sync but a full 1:1 sweep is worth doing before the final
  GA cut.
- **SMS (Twilio), ONVIF auto-discovery, HA MQTT discovery, NVR-lite,
  time-of-day deterrent conditions, per-class cooldown, per-rule cooldown
  override, mobile-responsive CSS, UI polish / branding** — feature work,
  not hardening. All remain on Future Ideas.

---

## v1.14.4 — training-data export tuning (planned)

Organic items surfaced while preparing the first heron-tuned model
training run (2026-04-29). All three are tweaks to the existing
labeling/export flow — not new surface — so they fit a 1.14.x patch
rather than a feature minor.

1. **Export false positives as background samples.** Widen
   `_EXPORTABLE_WHERE` in `services/web/src/db.py` to include
   `feedback = 'false_positive'`. In `services/web/src/routes/training.py`
   export loop, write the image but emit an empty `.txt` label file for
   those rows. YOLO treats image-with-no-labels as the canonical
   "background — no target here" signal; the training pipeline currently
   drops these on the floor even though they're the highest-signal
   background samples (the model thought it saw something and was
   wrong). Enables third-party heron/duck/raccoon datasets to train
   against actual pond/yard backgrounds without manual annotation.
2. **Training dashboard groups by effective class.**
   `db.get_feedback_stats` currently groups the per-class chart by the
   model's `class_name`, so `wrong_class` corrections to "heron" / "duck"
   / "raccoon" land under whatever the model originally guessed (person,
   bird, etc.). Group by the same `_effective_class` rule the export
   uses (corrected_class when feedback is `wrong_class`, else
   class_name) so corrected labels actually appear in the dashboard, and
   add a false-positive count column so background-sample volume is
   visible alongside positive-class counts.
3. **`training/README.md` updates.** Document the third-party dataset
   merge workflow (drop `images/` + `labels/` into the extracted zip,
   reconcile class indices in `data.yaml`); document that false
   positives become background samples; document the corrected_class
   bbox gotcha — the bbox stored on a `wrong_class` event is the model's
   *original* detection, so the corrected label is only useful when the
   model detected near the right place. UI affordance to redraw the
   bbox while correcting class is the v1.15 follow-up below.

Plus #131 (camera reconnect notice + flap suppression) bundled into
the same patch — see GitHub.

---

## v1.15 — exclusion zone editor + label-correction tooling (planned)

Feature work, not hardening — bumps the minor.

1. **Polygon exclusion zones with edit affordance.** Replace the
   rect-only zone tool with a polygon canvas tool, support editing
   existing zones (currently delete-and-recreate), and migrate stored
   rect zones into the polygon representation. Per-zone enable/disable
   toggle and zone labels (so reports can say "suppressed by 'pump
   area'") fold in naturally with the editor work. Inclusion zones
   (the inverse — whitelist regions where detection is active) added
   if nearly free once polygons exist. Closes #132.
2. **Redraw bbox on feedback.** Organic follow-up to the v1.14.4
   training-export tuning. When marking `wrong_class`, allow the user
   to redraw the bbox so the corrected label is on the right pixels.
   Currently the stored bbox is the model's original detection, which
   limits training value when the model boxed the wrong object
   entirely. Same canvas tooling as the polygon editor — natural
   pairing, which is why this is bundled here rather than backported
   to v1.14.x.

---

## Future Ideas (Unprioritized)

- Twilio SMS notifications — paid per-message, but works on any phone without an app
- Per-rule cooldown override — extend deterrent rules with an optional cooldown override, e.g. "heron: 10s, raccoon: 5min" without creating separate groups. v0.13.3 already supports per-group cooldown; this would add a per-match-row override. Deferred from 0.13.3 to keep scope focused.
- Time-of-day conditions on deterrent rules — e.g. "raccoon at night only". Originally listed under v0.13.x; scoped out of the v0.13.3 rule engine.
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
- ~~Config secrets at rest~~ — ✓ shipping in v1.14.0 (envelope encryption of sensitive fields via Fernet, key in `/data/secret_key`).
