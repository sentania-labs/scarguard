# ScarGuard — Roadmap

Active and planned features. Each item includes acceptance criteria. Completed features (1–17, 18–22, 24–25) are in [ROADMAP_ARCHIVE.md](ROADMAP_ARCHIVE.md).

---

## Feature 23: Scheduled Summary Reports

Daily or weekly email digest summarizing detection activity — the "check over coffee" report.

**Acceptance criteria:**
- Configurable schedule: daily, weekly, or disabled (default: disabled)
- Config section: `system.summary_report` with `enabled`, `frequency` (daily/weekly), `channels` (list of notification channel names to send the report to — email and/or Discord)
- Report contents: total detections by class, detections by camera, longest visit duration (if Feature 19 is implemented), camera uptime summary (if Feature 20 is implemented), system health snapshot (avg CPU/GPU usage)
- Report covers the previous day (daily) or previous 7 days (weekly)
- For email channels: formatted HTML email with inline summary table
- For Discord channels: formatted embed message
- Report generation runs as a scheduled task in the notifier or web service
- Configurable via web UI Settings

---

## Feature 26: Event Record Pruning & Retention

Extend snapshot retention to also prune old detection event records from SQLite. Prevents unbounded DB growth. Coordinate with the training pipeline to protect labeled data.

**Acceptance criteria:**
- Change default `system.snapshot_retention_days` from 30 to 90 (training dataset collection needs a longer window)
- New config field: `system.event_retention_days` (default: matches `snapshot_retention_days`)
- Pruning deletes event records older than retention period from `detection_events` table
- **Protected events:** Events with feedback labels (`correct`, `false_positive`, `wrong_class`) are NEVER pruned — they are training data. Only unlabeled events are eligible for pruning.
- Pruning runs on the same daily schedule as snapshot cleanup
- Visit records (Feature 19) follow the same retention policy
- Metrics data (Feature 21) has its own independent retention (`metrics_retention_days`)
- Dashboard or training page shows count of protected (labeled) vs pruneable events
- `event_retention_days: 0` disables event pruning (keep forever, matching snapshot behavior)

---

## Cleanup / Deprecation

- **Remove legacy SSL→TLS migration** (target v1.0 or v0.12) — `_migrate_ssl_to_tls()` in `main.py` and the legacy `ssl:` fallback in `caddy-entrypoint.sh`. Added in v0.9 to support users upgrading from the old `ssl:` config section. Safe to remove once enough releases have passed.
- **Remove legacy flat notification keys** (target v0.13.x) — `notifications.discord` and `notifications.email` flat config keys. Deprecated in v0.10 with log warnings. Users should migrate to `notifications.channels` named channel format. Code to remove: fallback branch in `build_notifiers()` (notifier `main.py`), `DiscordConfig`/`EmailConfig` models in web `config_model.py`, legacy form sections in `config.html`, legacy write in `routes/config.py`.

---

## Hardening (Opportunistic)

- **Redis authentication** — Add `requirepass` to Redis and pass credentials to detector/web/notifier via env var. Currently unauthenticated, relying on Docker network isolation. Low risk (single-tenant compose stack, no host port binding) but defense-in-depth says add auth. Touches Redis connection code in all three services.

---

## Future Ideas (Unprioritized)

- Twilio SMS notifications — paid per-message, but works on any phone without an app
- Per-class cooldown — different cooldown values per detected species (e.g. 30s for squirrels, 5min for herons)
- Mobile-friendly layout — responsive CSS for phone/tablet use; blocked on solving secure external access (reverse proxy, Tailscale, Cloudflare Tunnel)
- UI polish — logo, favicon, login screen branding. Japanese-inspired koi aesthetic. Use AI image generation for assets. Lowest priority.
- ONVIF camera auto-discovery — scan local network for RTSP cameras instead of manual URL entry. Helps non-UniFi users.
- Home Assistant MQTT discovery — auto-register ScarGuard as an HA device, beyond generic webhook
- Confidence auto-tuning — analyze feedback data to suggest optimal confidence thresholds per class
- Night/IR mode awareness — detect when cameras switch to IR and adjust confidence thresholds or flag detections accordingly
- NVR-lite — proxy live RTSP video + audio through the web UI (HLS or WebRTC). Significant scope; dedicated NVR tools (Frigate, Protect) already do this well.
- S3/Minio remote config backup — upload config backups to object storage for off-device redundancy
- Automated Orin runner/self-updates — CI pushes runner updates to Orin via SSH (parked)
