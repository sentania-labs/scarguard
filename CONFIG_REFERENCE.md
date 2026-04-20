# ScarGuard — Config & Detection Reference

## Config File Format (scarguard.yml)

```yaml
system:
  armed: true
  log_level: info
  timezone: "UTC"
  retention_days: 90             # days; applies to snapshots, events, visits, and metrics; 0 = keep forever (1-365)
  stats_interval: 5             # seconds between system stats collection (1-60)
  visit_timeout_seconds: 300    # gap before a visit session is closed (60-3600)
  training_nudge_threshold: 100 # labeled events before showing training nudge banner (10-10000)
  base_url: ""                  # external URL for feedback links in notifications (e.g. "https://scarguard.example.com")
  camera_health:
    alert_threshold_minutes: 10 # minutes offline before alerting (1-1440)
    debounce_seconds: 30        # ignore brief RTSP hiccups shorter than this (5-300)
  backup:
    max_backups: 50             # maximum number of config backups to keep (5-500)
    debounce_seconds: 180       # wait this long after config change before backup (30-600)
  # schedule:                        # optional — omit entirely for manual-only control
  #   enabled: false                 # toggle scheduling on/off without clearing times
  #   arm_time: "06:00"              # arm at 6 AM local time (HH:MM, 24-hour)
  #   disarm_time: "20:00"           # disarm at 8 PM local time
  #   use_solar: false               # true = arm at sunrise, disarm at sunset
  #   latitude: null                 # required when use_solar is true
  #   longitude: null                # required when use_solar is true
  # summary_report:                  # optional — scheduled digest notifications
  #   enabled: false                 # toggle digest on/off (default: disabled)
  #   frequency: daily               # daily | weekly (Mondays) | monthly (1st)
  #   time: "07:00"                  # HH:MM in system timezone
  #   channels: []                   # notification channel names to receive the digest

tls:
  mode: "off"                  # "off", "auto" (Let's Encrypt), or "manual" (own certs)
  domain: ""                   # required for auto mode
  cert_path: /config/certs/cert.pem   # for manual mode
  key_path: /config/certs/key.pem     # for manual mode

cameras:
  - name: pond-north
    rtsp_url: "rtsp://172.16.0.1:7447/STREAM_TOKEN_1"
    enabled: true
    resolution: 720
    # model_path: /models/heron-v2.pt         # optional — override global model
    # detect_classes: [great_blue_heron]       # optional — override global target_classes
    exclusion_zones:
      - x: 320
        y: 180
        w: 80
        h: 120
        label: "heron decoy"
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
  frame_skip: 2

  # Per-camera notification rules (defined inside each camera block)
  # NOTE: v0.13.3 renamed `action_rules` → `notification_rules`.  Pre-v0.13.3
  # configs are auto-migrated on load.
  notification_rules:
    - class_name: great_blue_heron
      channels: [pond-alerts, owner-email]
    - class_name: bird
      channels: [pond-alerts]
    - class_name: "*"        # catch-all
      channels: [pond-alerts]

  # Per-camera deterrent rules (v0.13.3+, defined inside each camera block).
  # First-match-wins on class_name; empty list = no deterrent (explicit opt-in).
  # Group names reference entries in deterrent.groups (below).
  deterrent_rules:
    - class_name: great_blue_heron
      groups: [thermonuclear]        # the full battery of deterrents
    - class_name: raccoon
      groups: [minor]                # single siren

  # Optional per-camera overrides (v0.13.3+).  Any omitted key inherits the
  # corresponding global `detection.*` value.
  confidence_threshold: 0.40         # higher than global for this camera's fine-tuned model

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
    - name: deterrent-webhook          # points to a downstream system (e.g. Scar's Revenge)
      type: webhook
      url: "http://192.168.1.x/api/fire"
      method: POST
      auth_token: "YOUR_TOKEN"         # optional Bearer token
      enabled: false
    - name: home-assistant
      type: webhook
      url: "http://homeassistant.local:8123/api/webhook/scarguard"
      method: POST
      enabled: false
redis:
  host: redis
  port: 6379
```

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
| `start_time` | TEXT | ISO 8601 UTC — first detection |
| `end_time` | TEXT | ISO 8601 UTC — last detection |
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
| `key` | TEXT | Primary key — state key name |
| `value` | TEXT | State value |

## RTSP Notes

ScarGuard works with any camera that provides an RTSP stream. The notes below reflect the reference setup (UniFi cameras).

- UniFi Protect: RTSP must be enabled per-camera in the Protect UI
- RTSP URL format varies by vendor — UniFi example: `rtsp://172.16.0.1:7447/<stream_token>`
- Use a 720p substream for inference where available — 4K wastes GPU cycles
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

The three roles are **not strictly hierarchical** — a viewer sees *more* than
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

- `cameras[].rtsp_url` — RTSP URLs with embedded auth tokens
- `notifications.discord.webhook_url` — Discord webhook (itself a credential)
- `notifications.email.smtp_pass` — SMTP password
- `notifications.channels[].webhook_url` — per-channel Discord webhook
- `notifications.channels[].smtp_pass` — per-channel email password
- `notifications.channels[].auth_token` — webhook Bearer token
- `notifications.channels[].token` — ntfy Bearer token
- `notifications.channels[].password` — ntfy Basic auth password
- `notifications.channels[].headers` — custom HTTP headers (may carry auth)

The raw-YAML tab (`GET /config/raw`) is admin-only and returns 403 for
viewers, because there's no lossless way to redact arbitrary YAML while
keeping the structure valid for round-tripping.

### Last-admin protection

The user-management routes refuse to delete, disable, or demote the last
active admin. Attempting any of those returns a `400 cannot demote the
last admin — promote another user first.` so a misclick can't orphan the
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

The `deterrent` section configures the deterrent service — automated physical deterrence via Tuya Cloud API. See [TUYA_SETUP.md](TUYA_SETUP.md) for obtaining API credentials.

**v0.13.3 changed the firing model.** Prior to v0.13.3 any detection fired
every enabled device.  From v0.13.3 onward, the deterrent service fires only
**groups** explicitly referenced by a per-camera **`deterrent_rules`** entry
(see the per-camera config section).  There is no default group — users
deliberately opt-in each camera/class combination that should actuate.

Firing is gated by two cooldown layers:

1. **Per-group cooldown** (`deterrent.groups[].cooldown_seconds`) — prevents
   the same group from firing twice in rapid succession.
2. **Global cooldown** (`deterrent.defaults.cooldown_seconds`) — prevents
   *any* actuation (across all groups) from firing more often than this.

| Key | Type | Default | Description |
|---|---|---|---|
| `deterrent.enabled` | bool | `false` | Master toggle for physical deterrence |
| `deterrent.tuya.api_key` | str | — | Tuya IoT Platform Access ID |
| `deterrent.tuya.api_secret` | str | — | Tuya IoT Platform Access Secret |
| `deterrent.tuya.api_region` | str | `"us"` | Tuya data center: `us`, `eu`, `cn`, `in` |
| `deterrent.devices[].name` | str | — | Human-readable device name |
| `deterrent.devices[].device_id` | str | — | Tuya device ID |
| `deterrent.devices[].type` | str | — | One of: `sprinkler`, `light`, `sound`, `plug` |
| `deterrent.devices[].enabled` | bool | `true` | Whether this device participates in deterrence |
| `deterrent.devices[].dp_code` | str | (auto) | Override the default DP code for on/off |
| `deterrent.groups[].name` | str | — | **v0.13.3**: Unique group name (referenced from per-camera `deterrent_rules.groups`) |
| `deterrent.groups[].devices` | list[str] | `[]` | **v0.13.3**: Device names (from the registry) fired by this group. A device may appear in multiple groups. |
| `deterrent.groups[].cooldown_seconds` | int | `60` | **v0.13.3**: Minimum seconds between firings of this group (on top of the global cooldown). |
| `deterrent.groups[].device_count_range` | list[int] \| null | inherit | **v0.13.3**: Override `defaults.device_count_range` for this group. `null` to inherit. |
| `deterrent.groups[].spray_duration_range` | list[float] \| null | inherit | **v0.13.3**: Override spray duration. |
| `deterrent.groups[].inter_device_delay_range` | list[float] \| null | inherit | **v0.13.3**: Override inter-device delay. |
| `deterrent.groups[].pre_delay_range` | list[float] \| null | inherit | **v0.13.3**: Override pre-delay. |
| `deterrent.defaults.device_count_range` | list[int] | `[1, 4]` | Min/max devices to fire per event (group override available) |
| `deterrent.defaults.spray_duration_range` | list[float] | `[3.0, 8.0]` | Min/max seconds each device stays on |
| `deterrent.defaults.inter_device_delay_range` | list[float] | `[1.0, 5.0]` | Min/max seconds between device activations |
| `deterrent.defaults.pre_delay_range` | list[float] | `[0.0, 3.0]` | Min/max seconds before sequence starts |
| `deterrent.defaults.cooldown_seconds` | int | `60` | **Global** cooldown — minimum gap between *any* two actuations across all groups. Group cooldowns stack on top. |
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

Override per-device with `dp_code` if your device uses a different DP.

## Service Communication

- **Between services:** Redis pub/sub. Detector publishes detection events; notifier, deterrent, and web UI subscribe.
- **Config:** All services read from mounted `config/scarguard.yml` in external data directory. Web UI can write to it. Detector, notifier, and deterrent auto-reload on config file changes.
- **Database:** SQLite at `data/scarguard.db`, shared volume between web, detector, and deterrent.
