# ScarGuard: Config & Detection Reference

## Config File Format (scarguard.yml)

```yaml
system:
  armed: true
  log_level: info                # debug | info | warning | error; all services honor it, the training controller applies changes without a restart
  timezone: "UTC"
  retention_days: 90             # days; applies to snapshots, events, visits, and metrics; 0 = keep forever (0 or 1-365)
  stats_interval: 5             # seconds between system stats collection (1-60)
  visit_timeout_seconds: 300    # gap before a visit session is closed (60-3600)
  training_nudge_threshold: 100 # labeled events before showing training nudge banner (10-10000)
  uploads:
    model_mb: 500              # model file limit in MiB (1-16384)
    dataset_mb: 500            # training dataset/video file limit in MiB (1-16384)
  auth:
    enabled: true               # master toggle for authentication (default: true)
    session_timeout_hours: 24   # session expiry (default: 24)
    max_login_attempts: 5       # lockout after N failed attempts (default: 5)
    lockout_duration_minutes: 15 # lockout duration in minutes (default: 15)
    require_api_auth: false     # require Bearer token for API endpoints (default: false)
    nonadmin_rearm_minutes: 30  # auto-rearm after non-admin disarm (0 = disabled) (default: 30)
  camera_health:
    alert_threshold_minutes: 10 # minutes offline before alerting (1-1440)
    debounce_seconds: 30        # ignore brief RTSP hiccups shorter than this (5-300)
  backup:
    max_backups: 50             # maximum number of config backups to keep (5-500)
    debounce_seconds: 180       # wait this long after config change before backup (30-600)
  # schedule:                        # optional - omit entirely for manual-only control
  #   enabled: false                 # toggle scheduling on/off without clearing times
  #   arm_time: "06:00"              # arm at 6 AM local time (HH:MM, 24-hour)
  #   disarm_time: "20:00"           # disarm at 8 PM local time
  #   use_solar: false               # true = arm at sunrise, disarm at sunset
  #   latitude: null                 # required when use_solar is true
  #   longitude: null                # required when use_solar is true
  # summary_report:                  # optional - scheduled digest notifications
  #   enabled: false                 # toggle digest on/off (default: disabled)
  #   frequency: daily               # daily | weekly (Mondays) | monthly (1st)
  #   time: "07:00"                  # HH:MM in system timezone
  #   channels: []                   # notification channel names to receive the digest

tls:
  mode: "off"                  # "off", "auto" (Let's Encrypt), or "manual" (own certs)
  domain: ""                   # required for auto mode (public FQDN only); manual/off also accept a LAN host or IPv4, optionally :port
  cert_path: /config/certs/cert.pem   # for manual mode; a file under /config/ (letters, digits, . _ - and /)
  key_path: /config/certs/key.pem     # for manual mode; a file under /config/ (letters, digits, . _ - and /)

cameras:
  - name: pond-north
    rtsp_url: "rtsp://172.16.0.1:7447/STREAM_TOKEN_1"
    enabled: true
    resolution: 720
    model_path: null              # optional - per-camera model override (default: null = use global detection.model_path)
    detect_classes: null          # optional - per-camera class filter (default: null = use global detection.target_classes)
    confidence_threshold: null    # optional - per-camera confidence override (default: null = use global detection.confidence_threshold)
    # Exclusion zones use normalized polygon coordinates (0.0 to 1.0 relative to frame).
    exclusion_zones:
      - points: [[0.50, 0.40], [0.62, 0.40], [0.62, 0.58], [0.50, 0.58]]
        label: "heron decoy"
    # Per-camera notification rules - route detections from this camera to
    # specific named channels.  Rules are evaluated top-down; first match wins.
    # Omit to notify every enabled channel.
    notification_rules:
      - class_name: great_blue_heron
        channels: [pond-alerts, owner-email]
      - class_name: bird
        channels: [pond-alerts]
      - class_name: "*"        # catch-all
        channels: [pond-alerts]
  - name: pond-south
    rtsp_url: "rtsp://172.16.0.1:7447/STREAM_TOKEN_2"
    enabled: true
    resolution: 720
    exclusion_zones: []

detection:
  model_path: /models/best.engine
  confidence_threshold: 0.25
  target_classes:
    - great_blue_heron
    - green_heron
    - duck
    - raccoon
  cooldown_seconds: 30
  frame_skip: 2                    # integer >= 1; invalid values are rejected

  # Per-camera overrides and notification_rules / deterrent_rules belong in
  # each camera block. When an override is null, the corresponding global
  # `detection.*` value is inherited.
  # In the UI, list fields like detect_classes, notification_rules.channels,
  # and deterrent_rules.groups use a chip-autocomplete control (v0.13.4+) -
  # typos render as amber "unknown" chips.  Unresolved references produce an
  # advisory warning on save (save still succeeds).

notifications:
  channels:
    - name: phone-alerts
      type: ntfy
      server: "https://ntfy.sh"       # or self-hosted ntfy URL
      topic: "scarguard-alerts"
      token: ""                        # optional Bearer token for authenticated topics
      # username: ""                   # alternative: Basic auth
      # password: ""
      priority: 3                      # 1 (min) to 5 (max/urgent)
      include_snapshot: true
      enabled: true
    - name: lan-ntfy                   # self-hosted ntfy on the LAN
      type: ntfy
      server: "http://192.168.1.60:8080"
      topic: "scarguard-alerts"
      allow_internal: true             # LAN destination opt-in (default false)
      enabled: false
    - name: deterrent-webhook          # points to a downstream system (e.g. Scar's Revenge)
      type: webhook
      url: "http://192.168.1.x/api/fire"
      method: POST
      auth_token: "YOUR_TOKEN"         # optional Bearer token
      allow_internal: true             # LAN destination opt-in (default false)
      enabled: false
    - name: home-assistant
      type: webhook
      url: "http://homeassistant.local:8123/api/webhook/scarguard"
      method: POST
      allow_internal: true             # resolves to a LAN address
      enabled: false
    - name: lan-relay                  # SMTP relay with a private CA
      type: email
      smtp_host: "mail.lan.example"
      smtp_port: 587                   # 465 = implicit TLS; any other port = STARTTLS required
      smtp_ca_file: /config/certs/smtp-ca.pem   # optional extra trusted CA (PEM)
      allow_internal: true
      # smtp_insecure_plaintext: true  # INSECURE opt-in: plaintext relay, no TLS
      to_addresses: [you@example.com]
      enabled: false
redis:
  host: redis
  port: 6379
```

### Validation before a config replaces the live one (FDY-0569)

- **TLS values** are interpolated into the generated Caddyfile, so they are
  allowlisted by `shared/tls_safety.py`. With `mode: auto`, `tls.domain` is
  required and must be a fully qualified DNS hostname (at least two labels,
  letters/digits/hyphens only, no IP, wildcard or port); in `manual`/`off` it
  only builds feedback links, so a LAN hostname or IPv4 address with an
  optional `:port` is also accepted. `cert_path`/`key_path` must be files
  under `/config/` (the only directory Caddy mounts) named with letters,
  digits, `.`, `_`, `-` and `/`, no `..`. Whitespace, newlines, braces and
  quotes are refused everywhere. The structured form, the raw-YAML editor and
  config restore refuse anything else, and the Caddy entrypoint applies the
  same rules again to hand edits. If a stored `tls` section fails these rules
  (for example an older config with `mode: auto` and an IP), the form shows
  defaults and an unrelated save keeps the stored section unchanged and
  returns a warning, rather than silently switching HTTPS off.
- **Raw-YAML saves and backup restores** validate the whole document against
  the same schema as the structured form (plus the deterrent range limits)
  before writing. Errors name the field and rule, never the value.
- **Restore** (`/admin/backups/<name>/restore`) additionally requires that every
  sensitive field can be stored encrypted under the existing `/data/secret_key`:
  plaintext secrets in the backup are encrypted with it, and the restore is
  refused if the key is missing or the backup's `enc:v1:` values were made
  with a different key. The current config is saved as a uniquely named
  `scarguard_<timestamp>_pre-restore.yml` backup first (the restore is refused
  if that fails), then the new file is written to a temporary file, fsynced
  and renamed over `scarguard.yml`, so an interrupted write keeps the old one.
  The restored file is re-serialized from the parsed YAML (comments in the
  backup are not kept). A structured save that lands in the moment between
  the pre-restore backup and the write is overwritten by the restore.
- **Caddy reload**: when `scarguard.yml` changes, `config/caddy_config.py`
  renders the new Caddyfile, runs `caddy validate` on it, swaps it in
  atomically (keeping `/etc/caddy/Caddyfile.last-good`) and reloads. Invalid
  values, a malformed `scarguard.yml`, a failed validation or a failed reload
  all keep the running config. At container start the same failures fall back
  to HTTP-only on :80 so the UI stays reachable to fix them. The rendered
  file always carries the `system.uploads` request-body caps (see
  [Upload limits and CSRF](#upload-limits-and-csrf-fdy-0568)); the fallback
  keeps the configured values when they are in range and uses 500 MiB otherwise.
- **`system.config_api.enabled`** is not supported. The `config-api` service
  is an unauthenticated scaffold whose write routes return 501, so routing
  settings writes to it would break every save. Caddy ignores the flag (and
  logs that it did), and web refuses saves and restores that set it to `true`;
  a value already on disk does not block structured-form saves.

### Notification destinations and attachments (FDY-0571)

Notification channels send credentials (Discord webhook tokens, bearer
tokens, ntfy and SMTP passwords) and snapshot images, so every destination
and attachment is checked.

**Destination policy** (`shared/url_safety.py`, the same rules at save and
at send):

| Destination | Policy |
|---|---|
| Loopback (127/8, ::1), link-local incl. cloud metadata (169.254.169.254 and other known metadata IPs), multicast, unspecified/reserved, the Docker bridge 172.17.0.0/16, the ScarGuard compose network 172.24.0.0/16, ScarGuard's own service names (`redis`, `web`, ...) and `localhost` | **Always refused**, even with `allow_internal: true` |
| LAN ranges 10/8, 172.16/12, 192.168/16, 100.64/10 (CGNAT, e.g. Tailscale), IPv6 ULA fc00::/7 | Refused unless the channel sets `allow_internal: true` |
| Globally routable addresses | Allowed |

- Webhook and ntfy URLs must be `http`/`https`; SMTP and URL ports must be 1-65535.
- **Save time**: the structured form, raw-YAML editor and backup restore refuse
  an enabled channel the notifier would refuse. These checks are static (no
  DNS): scheme, port, literal and legacy IP forms, service names, flag types
  and an absolute `smtp_ca_file` path. A hostname such as
  `homeassistant.local` passes the save check and is judged by address at
  send time. Errors name the channel and field (and at most an IP literal or
  service name); they never echo a URL path, query or credential. When a
  structured save fails for a destination and another reason at once, only
  the destination problem is reported.
- **Send time**: the notifier resolves the host once, checks every returned
  address, then connects only to those addresses (TLS SNI and certificate
  hostname checks still use the configured name). A DNS answer that changes
  between check and connect (DNS rebinding) therefore cannot redirect the
  request. HTTP redirects are never followed and proxy environment
  variables are ignored. A channel whose stored destination fails the
  static check is disabled at notifier start (logged); a send whose
  resolved address is refused is dropped and logged.
- Discord channels never accept `allow_internal` (Discord is always public).

**Per-channel settings** (all configurable on the channel card in
Settings > Notifications, all off by default):

| Key | Channel types | Meaning |
|---|---|---|
| `allow_internal` | webhook, ntfy, email | `true` permits LAN destinations listed above. Must be a boolean: a string like `"yes"` is refused on save, and a hand-edited one makes the notifier disable the whole channel (logged). |
| `smtp_ca_file` | email | Absolute path to an extra trusted CA certificate (PEM) for a relay with a private CA, e.g. `/config/certs/smtp-ca.pem` (the notifier mounts the config volume read-only at `/config`). Added to the system trust store and certifi; the notifier disables the channel (logged) if the file is missing or is not a readable PEM certificate. |
| `smtp_insecure_plaintext` | email | **INSECURE** explicit opt-in for an intentional plaintext relay: STARTTLS is skipped and mail plus the SMTP password are sent unencrypted. Shown as an INSECURE badge on the config page; the notifier logs a warning. Has no effect on port 465. |

**SMTP transport**: port 465 uses implicit TLS; every other port (587, 25,
2525, ...) requires STARTTLS before AUTH, and a server that does not offer it
is refused before any credential is sent unless `smtp_insecure_plaintext` is
`true`. Certificates and hostnames are always verified; a self-signed or
private-CA relay needs `smtp_ca_file`. Previously only port 587 used
STARTTLS (without certificate verification) and other non-465 ports such as
25 sent plaintext; such relays now need STARTTLS with a verifiable certificate (or `smtp_ca_file`), or the
explicit plaintext opt-in.

**Structured save is strict**: the channel editor always sends
`allow_internal` (webhook, ntfy, email) and `smtp_ca_file` /
`smtp_insecure_plaintext` (email), and what it sends is stored: unticking a
box stores `false` and an empty CA path removes `smtp_ca_file`. An omitted
setting is never read as "keep what was stored": with nothing stored it means
off, and when the stored channel has it turned on the save is refused with
"was not sent; reload the config page" (a browser still running a cached
older `config.js`). A LAN channel saved without the opt-in is refused with a
message naming the channel and `allow_internal`. A hand-edited
`allow_internal` on a Discord channel is dropped on the next structured save.

**Snapshot attachments**: a snapshot is attached only if it resolves
(symlinks included) to a regular file inside `SNAPSHOT_DIR`
(`/data/snapshots`), has a `.jpg`/`.jpeg`/`.png` suffix, really is a JPEG or
PNG of that type (Pillow verifies the bytes), and is at most 20 MB and 40
megapixels. Anything else is refused and the notification is sent without an
image.

**Limitations**: the save-time check cannot resolve hostnames, so a
hostname that resolves to a refused address is only caught at send time
(logged by the notifier, not shown on save). The config page's
destination-security table shows each channel's saved settings and static
problems.

### Notification delivery queues (FDY-0572)

Each enabled channel in `notifications.channels` is delivered by its own
bounded queue and thread (`services/notifier/src/channel_dispatcher.py`).
The Redis subscriber never waits on a sender or the network - it hands each
event to the channel queues (and, when a queue is full, writes it to the
local retry file) - so an SMTP relay that accepts the connection and then
hangs delays that email channel alone: Discord, ntfy and webhook channels,
and the subscriber itself, keep going. Within a channel, a live alert is
delivered before the channel continues through its retry backlog. Nothing
in `scarguard.yml` or the channel cards changes; the Discord and email
senders and their settings are the ones validated before.

| Bound | Default | Behaviour when reached |
|---|---|---|
| Live queue per channel | 50 events (`NOTIFIER_CHANNEL_QUEUE_SIZE`) | The event is written to the disk retry queue instead of being dropped (logged as `delivery queue full`, counter `overflowed`). |
| Delivery attempt deadline | 60 s (`NOTIFIER_SEND_DEADLINE`, seconds) | The event is queued for retry (`timed_out`) and the channel is *stalled* until that attempt ends on its own (the senders' socket timeouts guarantee it does). While stalled, further events for the channel go to the retry queue without a send (`deferred`), so connections never pile up on a hanging relay. If the late attempt still succeeds its retry entry is cancelled; a duplicate alert is possible only if the retry already fired. |
| Retry queue (`notification_queue.json` on the `scarguard-notifier` volume) | 500 entries, 24 h | Unchanged: oldest entry dropped when full (logged), entries older than 24 h discarded; backoff 30 s doubling to a 10-minute cap. Each channel's thread retries only its own entries, so one channel's retries never wait on another's. |

Both environment variables are read by the notifier container at start
(set them under the notifier service's `environment:` in a compose
override); they are operational tunables, not `scarguard.yml` settings, and
are not shown in the UI.

**Retry file**: every save writes `notification_queue.json.tmp` next to the
queue file, fsyncs it and renames it into place, so a crash, `docker stop`
timeout or power loss during a save leaves the previous complete queue, not
a truncated file that would have discarded every pending retry on the next
start. A leftover `.tmp` is ignored and removed at start.

**Shutdown and restart**: on SIGTERM the dispatcher immediately writes every
waiting event to the retry queue (one file write per channel), waits up to
5 s for sends already in progress and writes those too (an event that then
completes late cancels its own entry); this fits inside Compose's default
10 s stop grace. On the next start the file is loaded (`Resuming with N
notification(s) pending in retry queue: email=N`) and each channel's thread
delivers its entries once they are due. A channel removed from the config
has its waiting events persisted the same way; they wait in the retry queue
until a channel of that name is enabled again or they expire. An event for
a channel that has no delivery worker (removed by a reload that raced the
event) is written to the retry queue as well, never sent with the old
settings.

**Observability**: every outcome is logged with the channel name and the
running counter (`[email] delivery attempt exceeded 60s deadline - event
queued for retry`, `[email] channel stalled ...`, `[email] persisted N
pending notification(s) ...`, `Notification dispatcher stopped - retry
queue depth N: email={...}`); the per-channel counters are `submitted`,
`delivered`, `failed`, `timed_out`, `deferred`, `overflowed`, `retried`,
`persisted`. There is no UI for them yet.

**Limitations**: a send cannot be interrupted, so a stalled attempt holds
its thread until the sender's own socket timeouts fire (15 s per SMTP
command, 10 s per HTTP alert request, 15 s for digest requests) - the
deadline bounds when the event is handed to retry, not when the socket
closes. Digest reports
(`system.summary_report`) are still sent inline on the digest scheduler's
own thread, which never touches the subscriber. Verified with local fixture
relays (`services/notifier/tests/test_fdy_0572_regression.py`), not against
a production SMTP server or Discord.

## Detection Logic

1. Pull frames from each RTSP stream (OpenCV `VideoCapture`)
2. Run YOLO inference on GPU (`model.predict()`)
3. Filter results by target classes and confidence threshold
4. Apply cooldown dedup (don't fire 10 events for same heron standing there)
5. On new detection event:
   - Save to SQLite (timestamp, class, confidence, camera, snapshot path, bbox, frame_size)
   - Publish to Redis pub/sub channel `scarguard:detections`
   - Save clean snapshot frame to disk (no bbox annotation burned in)
6. Notifier picks up events from Redis and dispatches to configured channels
7. Web UI subscribes to Redis for live event feed via SSE
8. Web UI renders bbox overlay on snapshots using stored coordinates

## Detection Feedback & Training Pipeline

Events can be labeled via the web UI Events page:
- **Correct**: Detection was accurate
- **False Positive**: Detection was wrong (no animal present)
- **Wrong Class**: Animal was present but misidentified (provide corrected class)

Labeled events power the training pipeline:
- **Training Data** admin page shows per-class feedback stats
- **Export** generates YOLO-format dataset zip from confirmed detections
- **Training script** (`training/train.py`) fine-tunes YOLO on exported data
- **Model Evaluation** page compares two models side-by-side against labeled snapshots
- **Model Promotion** updates config and triggers hot-reload

### Training Configuration (`training:` section)

The `training:` YAML section configures the trainer service (dataset
preparation and on-device fine-tuning jobs). It is editable on the
Config page under the **Training** sub-tab (v1.16.7+).
`training.sources.roboflow.api_key` is sensitive and redacted in the UI
and raw-YAML views. It remains plaintext because the trainer reads
`scarguard.yml` directly and cannot decrypt secret-box values, the same
trade-off as camera RTSP URLs for the detector. Without a Roboflow
key, dataset preparation skips the Roboflow Universe sources and logs
a warning, heron training coverage depends on them.

Training jobs stop the detector process through the allowlisted lifecycle
controller before admission, then restore it only when that job owned the stop.
Failed jobs expose the newest valid `last.pt` beneath the training runs directory
as a user-selected Resume action; checkpoints are never resumed automatically.
Full sanitized stdout/stderr is retained under
`/data/training_workspace/logs/<job_id>.log` within the byte/age limits below.

| Key | Default | Description |
|---|---|---|
| `training.defaults.classes` | `[duck, heron, raccoon, person, dog, cat, plant]` | Ordered training class list, order defines model class indices. Overridable per job via the Classes field on the Training Jobs page. |
| `training.defaults.base_model` | `yolov8n.pt` | Base model for fine-tuning |
| `training.defaults.epochs` | `100` | Training epochs |
| `training.defaults.batch_size` | `2` | Batch size (Orin 8GB safe) |
| `training.defaults.image_size` | `480` | Training image size (Orin 8GB safe) |
| `training.defaults.patience` | `20` | Early-stopping patience |
| `training.defaults.val_split` | `0.15` | Validation holdout fraction |
| `training.defaults.workers` | `4` | Ultralytics data-loader workers. The Jetson Orin profile accepts 0-4. |
| `training.resources.min_mem_available_mb` | `1536` | Minimum host `MemAvailable` after detector teardown before training is admitted |
| `training.resources.min_swap_free_mb` | `512` | Minimum free swap when the host has swap configured |
| `training.logs.max_bytes` | `16777216` | Per-job durable stdout/stderr byte cap |
| `training.logs.retention_days` | `30` | Durable training-log retention window |
| `training.sources.roboflow.api_key` | `""` | Roboflow API key (SENSITIVE) |
| `training.sources.open_images.max_per_class` | `1500` | Open Images cap per class |
| `training.sources.open_images.workers` | `16` | Parallel download threads |
| `training.video.low_confidence` | `0.05` | Low-confidence pass threshold for video processing |
| `training.video.dedupe_iou` | `0.85` | IoU threshold for near-duplicate frame dedup |
| `training.video.dedupe_window` | `5` | Frame window for dedup |
| `training.video.background_sample_interval` | `10` | Every Nth frame of background uploads becomes a negative sample |

### Distractor Classes & Runtime Behavior

`person`, `dog`, `cat`, and `plant` are **distractor classes**: they are
trained into the model so it learns what *not* to call a heron (humans at
the pond were previously misclassified as herons), but they are not meant
to alert or deter. All runtime behavior is driven by existing class
config, no special handling:

- `detection.target_classes` is an explicit allowlist. Distractor classes
  not listed there are dropped at inference: no events, no snapshots.
  Deploying a 7-class model with an unchanged config needs no edits.
- **Never add `plant` to `target_classes`**: pond vegetation would fire
  constantly.
- To **log** a distractor (e.g. person) without notifications: add it to
  `target_classes`, then give the camera explicit `notification_rules`
  for the classes you *do* want notifications for, **without a wildcard
  rule**. A class matching no rule is suppressed. Note: a *matched* rule
  with `channels: []` means "notify all channels", that is not the
  suppression recipe.
- Deterrents are opt-in per class via `deterrent_rules`, so distractors
  can never trigger them unless explicitly configured.
- The Models page will show all 7 classes for a distractor-trained model
  while `target_classes` lists fewer, that is expected, not a
  misconfiguration.

### Database Columns (detection_events)

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Primary key |
| `timestamp` | TEXT | ISO 8601 UTC |
| `class_name` | TEXT | Detected class name |
| `confidence` | REAL | Detection confidence (0-1) |
| `camera_name` | TEXT | Camera name from config |
| `snapshot_path` | TEXT | Path to clean snapshot JPEG |
| `actions_triggered` | TEXT | JSON array of channel names |
| `bbox` | TEXT | JSON `[x1, y1, x2, y2]` pixel coords |
| `frame_size` | TEXT | JSON `[width, height]` of original frame |
| `feedback` | TEXT | `correct`, `false_positive`, `wrong_class`, or NULL |
| `corrected_class` | TEXT | Class name when feedback is `wrong_class` |
| `feedback_token` | TEXT | UUID4 hex token for one-click notification feedback (unique, 7-day expiry) |

### Database Tables: visit_sessions

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Primary key |
| `camera_name` | TEXT | Camera that recorded the visit |
| `class_name` | TEXT | Detected species |
| `start_time` | TEXT | ISO 8601 UTC, first detection |
| `end_time` | TEXT | ISO 8601 UTC, last detection |
| `duration_secs` | REAL | Visit length in seconds |
| `detection_count` | INTEGER | Number of detections in the session |

### Database Tables: system_metrics

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Primary key |
| `timestamp` | TEXT | ISO 8601 UTC |
| `cpu_pct` | REAL | CPU usage percentage (0-100) |
| `gpu_pct` | REAL | GPU usage percentage (0-100), NULL if no GPU |
| `gpu_temp` | REAL | GPU temperature in °C, NULL if unavailable |
| `ram_used_mb` | INTEGER | RAM used in MB |
| `ram_total_mb` | INTEGER | Total RAM in MB |
| `camera_data` | TEXT | JSON per-camera FPS/latency |

### Database Tables: app_state

| Column | Type | Description |
|---|---|---|
| `key` | TEXT | Primary key, state key name |
| `value` | TEXT | State value |

## RTSP Notes

ScarGuard works with any camera that provides an RTSP stream. The notes below reflect the reference setup (UniFi cameras).

- UniFi Protect: RTSP must be enabled per-camera in the Protect UI
- RTSP URL format varies by vendor: UniFi example: `rtsp://172.16.0.1:7447/<stream_token>`
- Use a 720p substream for inference where available: 4K wastes GPU cycles
- OpenCV `VideoCapture` handles RTSP natively; set `cv2.CAP_PROP_BUFFERSIZE` to 1 to reduce frame lag
- Reference cameras: UniFi G3 Flex and G5 Flex

## User Roles (v0.12.7+)

ScarGuard has three authentication roles, stored in the `role` column of the
`users` table (`auth.db`). Role is independent of `is_admin`, which is kept as
a legacy alias for one-release backwards compatibility.

| Role | Dashboard | Events / Visits / Stats | Admin pages (Training, Logs, Backups, Config) | Raw YAML | Writes (save config, arm, disarm, feedback) | User management |
|---|---|---|---|---|---|---|
| **user** | ✓ | ✓ | ✗ | ✗ | **disarm only** (with auto-rearm) + feedback | ✗ |
| **viewer** (read-only admin) | ✓ | ✓ | ✓ *(secrets masked)* | ✗ | ✗ | ✗ |
| **admin** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

The three roles are **not strictly hierarchical**, a viewer sees *more* than
a user (admin pages, the config form) but writes *less* (no disarm, no
feedback). The design makes "oversight without write risk" a first-class
option for family members or sysadmins who need visibility without the
ability to change anything or read plaintext secrets.

### Sensitive-field redaction for viewers

When a viewer loads `/config`, the server walks the YAML through
`services/web/src/config_redact.py:redact_config()` before it reaches the
browser. The following fields are replaced with `***REDACTED***` in the
cameras/channels JSON hydration, the Pydantic form data, and the
backups-diff view:

- `cameras[].rtsp_url`: RTSP URLs with embedded auth tokens
- `notifications.channels[].webhook_url`: per-channel Discord webhook
- `notifications.channels[].smtp_pass`: per-channel email password
- `notifications.channels[].auth_token`: webhook Bearer token
- `notifications.channels[].token`: ntfy Bearer token
- `notifications.channels[].password`: ntfy Basic auth password
- `notifications.channels[].headers`: custom HTTP headers (may carry auth)
- `deterrent.tuya.api_key` / `deterrent.tuya.api_secret`: Tuya Cloud API credentials

The raw-YAML tab (`GET /config/raw`) is admin-only and returns 403 for
viewers, because there's no lossless way to redact arbitrary YAML while
keeping the structure valid for round-tripping.

### Last-admin protection

The user-management routes refuse to delete, disable, or demote the last
active admin. Attempting any of those returns a `400 cannot demote the
last admin - promote another user first.` so a misclick can't orphan the
instance. See `auth.count_active_admins()` and the guards in
`services/web/src/routes/users.py`.

### Creating a viewer

From the admin UI: `/admin/users` → "Add User" → set Role = Viewer.
Existing users can be demoted or promoted via the role dropdown in the
user list (disabled for the currently-logged-in user as a defence-in-depth
measure).

## Arm/Disarm Modes

ScarGuard supports three operating modes controlled by `system.armed` and the optional `system.schedule` section:

- **Always armed (default):** Set `armed: true` and omit the `schedule` section. The system monitors continuously. Use the dashboard button to toggle manually at any time.
- **Always off:** Set `armed: false` and omit the `schedule` section. Camera threads still run but detections are not processed. Toggle via the dashboard when needed.
- **Scheduled:** Include a `schedule` section with `enabled: true` and `arm_time`/`disarm_time` (or `use_solar: true` with latitude/longitude). The system arms and disarms automatically at the configured times. Manual toggles from the dashboard override the schedule until the next scheduled transition. Set `enabled: false` to temporarily disable the schedule without clearing your configured times.

The schedule is entirely optional. If the `schedule` key is missing, `enabled` is false, or both time fields are empty, no automatic transitions occur and the armed state is under manual control only.

## Actuation (Physical Deterrence)

The `deterrent` section configures the deterrent service, automated physical deterrence via Tuya Cloud API. See [TUYA_SETUP.md](TUYA_SETUP.md) for obtaining API credentials.

**v0.13.3 changed the firing model.** Prior to v0.13.3 any detection fired
every enabled device.  From v0.13.3 onward, the deterrent service fires only
**groups** explicitly referenced by a per-camera **`deterrent_rules`** entry
(see the per-camera config section).  There is no default group, users
deliberately opt-in each camera/class combination that should actuate.

Firing is gated by two cooldown layers:

1. **Per-group cooldown** (`deterrent.groups[].cooldown_seconds`): prevents
   the same group from firing twice in rapid succession.
2. **Global cooldown** (`deterrent.defaults.cooldown_seconds`): prevents
   *any* actuation (across all groups) from firing more often than this.

| Key | Type | Default | Description |
|---|---|---|---|
| `deterrent.enabled` | bool | `false` | Master toggle for physical deterrence |
| `deterrent.tuya.api_key` | str | n/a | Tuya IoT Platform Access ID |
| `deterrent.tuya.api_secret` | str | n/a | Tuya IoT Platform Access Secret |
| `deterrent.tuya.api_region` | str | `"us"` | Tuya data center: `us`, `eu`, `cn`, `in` |
| `deterrent.devices[].name` | str | n/a | Human-readable device name |
| `deterrent.devices[].device_id` | str | n/a | Tuya device ID |
| `deterrent.devices[].type` | str | n/a | One of: `sprinkler`, `light`, `sound`, `plug` |
| `deterrent.devices[].enabled` | bool | `true` | Whether this device participates in deterrence |
| `deterrent.devices[].dp_code` | str | (auto) | Override the default DP code for on/off |
| `deterrent.groups[].name` | str | n/a | **v0.13.3**: Unique group name (referenced from per-camera `deterrent_rules.groups`) |
| `deterrent.groups[].devices` | list[str] | `[]` | **v0.13.3**: Device names (from the registry) fired by this group. A device may appear in multiple groups. |
| `deterrent.groups[].cooldown_seconds` | int | `60` | **v0.13.3**: Minimum seconds between firings of this group (on top of the global cooldown). |
| `deterrent.groups[].device_count_range` | list[int] \| null | inherit | **v0.13.3**: Override `defaults.device_count_range` for this group. `null` to inherit. |
| `deterrent.groups[].spray_duration_range` | list[float] \| null | inherit | **v0.13.3**: Override spray duration. |
| `deterrent.groups[].inter_device_delay_range` | list[float] \| null | inherit | **v0.13.3**: Override inter-device delay. |
| `deterrent.groups[].pre_delay_range` | list[float] \| null | inherit | **v0.13.3**: Override pre-delay. |
| `deterrent.groups[].group_duration_range` | list[float] \| null | inherit | **v1.17**: Override the group window. |
| `deterrent.defaults.device_count_range` | list[int] | `[1, 4]` | Min/max devices to fire per event (group override available) |
| `deterrent.defaults.spray_duration_range` | list[float] | `[3.0, 8.0]` | Min/max seconds each device stays on |
| `deterrent.defaults.inter_device_delay_range` | list[float] | `[1.0, 5.0]` | Min/max seconds between device activations |
| `deterrent.defaults.pre_delay_range` | list[float] | `[0.0, 3.0]` | Min/max seconds before sequence starts |
| `deterrent.defaults.group_duration_range` | list[float] \| null | `null` | **v1.17**: Min/max seconds the group keeps cycling. `null` = one pass. |
| `deterrent.defaults.cooldown_seconds` | int | `60` | **Global** cooldown, minimum gap between *any* two actuations across all groups. Group cooldowns stack on top. |
| `deterrent.reconcile_interval_sec` | int | `30` | Seconds between reconciliation polls. Detects stuck devices and force-OFFs any that report ON while not actively driven. 0 = disabled. |
| `deterrent.battery_monitor.enabled` | bool | `true` | Poll battery levels periodically |
| `deterrent.battery_monitor.check_interval_hours` | int | `24` | Hours between battery checks |
| `deterrent.battery_monitor.alert_threshold_percent` | int | `20` | Alert when battery drops below this |

### Default DP Codes by Device Type

| Device type | Default DP code | Notes |
|---|---|---|
| `sprinkler` | `switch_1` | Most Tuya sprinkler valves |
| `light` | `switch_led` | Tuya smart lights |
| `sound` | `switch` | Tuya sirens/alarms |
| `plug` | `switch_1` | Tuya smart plugs |

### Independent OFF watchdog

Every ON attempt first writes an HMAC-authenticated activation lease to Redis.
The lease deadline is derived from the requested, per-device activation (which
is clamped to `MAX_ACTUATION_SEC`) plus the bounded cloud admission time; it can
never be indefinite or exceed that envelope. If Redis or the dedicated signing
key is unavailable, the production deterrent service refuses to send ON.

The separate `off-watchdog` container reads the same configured device registry
and Tuya credentials, performs a conservative OFF sweep for every configured
device at startup, then sends OFF when a valid lease expires. It has no
activation API or true-valued cloud command. Successful normal OFF clears only
the matching lease, so it cannot erase a newer activation.

Lease records are non-expiring Redis keys protected by the stack's
`volatile-lru` policy; a separate expiring deadline marker makes eviction
fail-safe (a missing marker means OFF). The watchdog also tracks each observed
lease against a monotonic deadline, so a backward wall-clock correction cannot
make an activation indefinite. Startup OFF covers watchdog restarts.

This covers a deterrent process/container crash only while the host, Redis,
network, and Tuya Cloud remain reachable. Host failure, loss of power, or cloud
failure cannot be repaired by software on that host. Device firmware auto-off
is still required for those cases and has not been verified by Scott.

Override per-device with `dp_code` if your device uses a different DP.



### Randomization range limits (v1.17)

Every `*_range` below is `[low, high]`, both values inclusive, with
`low <= high`. Equal values are legal and mean a fixed, non-random value.

These drive physical hardware, so since v1.17 they are enforced rather than
merely suggested by the form:

| Range | Allowed interval |
|---|---|
| `device_count_range` | 1 to 20 |
| `spray_duration_range` | 0.5 to 60 seconds (`MAX_ACTUATION_SEC`) |
| `inter_device_delay_range` | 0 to 30 seconds |
| `pre_delay_range` | 0 to 30 seconds |

**Saving a value outside these is refused** with a 400 naming every field that
is wrong, rather than being silently accepted and truncated later. Before
v1.17 the only limits were `max` attributes on the form, so a hand-edited
`scarguard.yml` could set `pre_delay_range: [300, 300]` and produce several
minutes of hardware activity from one trigger.

**Loading an out-of-range value does not fail.** The deterrent service clamps
it at fire time and logs a warning. That asymmetry is deliberate: refusing a
bad write is right, but refusing to load an existing configuration would take
the deterrent out of service entirely over a value that can simply be bounded.
Fix the value at your leisure; the pond stays defended in the meantime.

### Group window (`group_duration_range`, v1.17)

By default a group fires one pass and stops: it picks a random subset of its
devices, sprays each once, and goes quiet for the cooldown. A heron that waits
out a three-second burst has not been deterred.

Set `group_duration_range` and the group keeps working the position for a
randomly chosen window instead. Each cycle re-picks the device subset and the
durations, so the pattern stays unpredictable, which is the same reason a
single pass is randomised at all.

```yaml
deterrent:
  groups:
    - name: thermonuclear
      devices: [Waterfall, Pump, Bridge]
      spray_duration_range: [3, 8]     # each device sprays 3-8s
      group_duration_range: [45, 90]   # group works the position 45-90s
```

With that config a detection sprays a rotating subset of the three devices for
somewhere between 45 and 90 seconds, rather than one 3-8 second burst.

**Bounds and behaviour**

- Omitted, `null` or `[0, 0]` means one pass, which is the pre-v1.17 behaviour.
  Existing configs are unaffected.
- The window is capped at `MAX_GROUP_ACTUATION_SEC` (300s). A larger value is
  rejected on save, and clamped with a warning if it reaches the deterrent
  service another way.
- The window is checked immediately **before** each activation, never during
  one. An in-flight spray always runs to its natural end, so the group can
  overshoot its window by up to one spray duration. No out-of-band OFF is ever
  sent, which is what keeps the per-activation watchdog authoritative.
- `cooldown_seconds` anchors to the **end** of the window, not the start. A
  60s window with a 60s cooldown gives 60s of quiet after the spraying stops.
  Anchoring at the start would make any cooldown shorter than the window
  silently meaningless.
- Each device activation is still individually capped at `MAX_ACTUATION_SEC`
  (60s). The window governs how many activations happen, never how long one
  lasts.


## Service Communication

- **Between services:** Redis pub/sub. Detector publishes detection events; notifier, deterrent, and web UI subscribe.
- **Config:** All services read from mounted `config/scarguard.yml` in external data directory. Web UI can write to it. Detector, notifier, and deterrent auto-reload on config file changes.
- **Database:** SQLite at `data/scarguard.db`, shared volume between web, detector, and deterrent.

### Emergency OFF command ordering (v1.17.1)

No configuration change is needed. Emergency OFF invalidates pending activations
before issuing OFF. An admitted ON finishes before OFF; a cancelled activation
sends no ON. This does not disarm future detections or eliminate cloud/device
latency. See `docs/EMERGENCY_OFF.md`.

## Event review controls (v1.17.1)

Bulk review and snapshot box correction use existing event feedback fields;
there are no new configuration keys. Corrected-class suggestions come from
`detection.target_classes`; operators may also type a class. Bulk wrong-class
feedback preserves each event's own corrected box. Correct/false-positive
feedback clears previous corrections. Viewer accounts have read-only access.

## Upload limits and CSRF (FDY-0568)

Configure `system.uploads.model_mb` and `system.uploads.dataset_mb` in the
Authentication section of the config UI or raw YAML. Both default to 500 MiB
(524,288,000 bytes), preserving 500 MB training uploads. Values from 1 through
16384 MiB are accepted, including quoted integers in raw YAML (for example,
`model_mb: "700"` or `model_mb: "700.0"`); the application and Caddy use the same limit. These settings
replace the undocumented
`MODEL_UPLOAD_MAX_BYTES` / `TRAINING_UPLOAD_MAX_BYTES` environment overrides.
Copy any intentional override into the corresponding YAML/UI setting before
upgrading. Upload copy chunks are fixed at 4 MiB; chunk-size environment
variables are no longer used.

Authentication and upload admin authorization run before body consumption.
Cookie-authenticated multipart requests require `X-CSRF-Token` matching the
signed CSRF cookie; native upload forms use JavaScript to send this header.
Small URL-encoded forms retain hidden-field CSRF support. Valid bearer-auth
requests retain their CSRF exemption. Disabling authentication on HTTP still
explicitly grants anonymous admin access, as before.

The application counts actual streamed bytes even with missing or false
Content-Length and rejects excess with HTTP 413. Each upload permits one
file, up to 16 text fields of 64 KiB each, 64 KiB of headers per part, and
at most 1 MiB of envelope
allowance beyond its file limit. The parser enforces the file limit while
spooling; it closes temporary files on rejection. Ordinary requests, including
TLS certificate uploads and URL-encoded forms (even at upload URLs), have a
1 MiB request cap.
Certificate/key validation retains its existing 64 KiB per-item limit.

Caddy applies matching request caps from the same YAML on config reload.
A limit increase can briefly remain subject to the old proxy cap until the
reload completes. Uploads spool to temporary disk before chunked destination
writes; allow space for approximately two copies of a maximum-size file.
A memory-backed `/tmp` consumes RAM for the spool: provision disk-backed
temporary storage for large uploads. Limits bound individual requests, not
aggregate disk use from concurrent authorized uploads.

## Reliability & Failure Handling

If saving a snapshot frame to disk or recording an event to the SQLite database fails, the detection event is still published to Redis to ensure safety alerts are not silently suppressed. However, the event will not contain a `feedback_token` and its `snapshot_path` will be null, and downstream notification templates will omit those components. Feedback tokens are only issued for fully persisted events.

During Redis connectivity outages, detection events and health alerts are buffered locally. Buffered detection events are dropped if they remain un-published for more than 60 seconds (a stale-event window) to avoid flooding the downstream channels with outdated motion alerts once connectivity is restored. Health alerts, however, are kept pending indefinitely until publication succeeds, ensuring no offline transitions are lost.

On the notifier side each channel is delivered by its own bounded queue with a per-attempt deadline, and failed, timed-out or overflowed events go to the disk-backed retry queue, which is written atomically and reloaded on restart. See "Notification delivery queues (FDY-0572)" above for the bounds and what each outcome looks like in the logs.
