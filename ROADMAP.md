# ScarGuard — Roadmap

Active and planned features. Each item includes acceptance criteria. Completed features (1–17, 18–22, 23–26) are in [ROADMAP_ARCHIVE.md](ROADMAP_ARCHIVE.md).

---

## Cleanup / Deprecation

- **Remove legacy SSL→TLS migration** (target x.12.x) — `_migrate_ssl_to_tls()` in `main.py` and the legacy `ssl:` fallback in `caddy-entrypoint.sh`. Added in v0.9 to support users upgrading from the old `ssl:` config section. Safe to remove once enough releases have passed.
- **Remove legacy flat notification keys** (target x.13.x)
- **Remove retention_days migration code** (target x.14.x) — auto-rewrite of `snapshot_retention_days`/`metrics_retention_days` → `retention_days` added in v0.11. Safe to remove once enough releases have passed. — `notifications.discord` and `notifications.email` flat config keys. Deprecated in v0.10 with log warnings. Users should migrate to `notifications.channels` named channel format. Code to remove: fallback branch in `build_notifiers()` (notifier `main.py`), `DiscordConfig`/`EmailConfig` models in web `config_model.py`, legacy form sections in `config.html`, legacy write in `routes/config.py`.

---

## Hardening (Opportunistic)

- **Redis authentication** — Add `requirepass` to Redis and pass credentials to detector/web/notifier via env var. Currently unauthenticated, relying on Docker network isolation. Low risk (single-tenant compose stack, no host port binding) but defense-in-depth says add auth. Touches Redis connection code in all three services.

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
