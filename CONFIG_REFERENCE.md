# ScarGuard — Config & Detection Reference

## Config File Format (scarguard.yml)

```yaml
system:
  armed: true
  log_level: info
  timezone: "UTC"
  snapshot_retention_days: 30   # days; 0 = keep forever
  stats_interval: 5             # seconds between system stats collection (1-60)
  # schedule:                        # optional — omit entirely for manual-only control
  #   enabled: false                 # toggle scheduling on/off without clearing times
  #   arm_time: "06:00"              # arm at 6 AM local time (HH:MM, 24-hour)
  #   disarm_time: "20:00"           # disarm at 8 PM local time
  #   use_solar: false               # true = arm at sunrise, disarm at sunset
  #   latitude: null                 # required when use_solar is true
  #   longitude: null                # required when use_solar is true

# SSL is off by default. Run setup.sh to generate a self-signed cert.
# cert_path / key_path are container-internal paths; certs live inside the
# config volume at /config/certs/.
ssl:
  enabled: false
  cert_path: /config/certs/cert.pem
  key_path: /config/certs/key.pem
  https_only: false              # true = disable plain HTTP on port 8080
  # keyfile_password: ""        # only if private key is passphrase-protected

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

action_rules:
  - match:
      class: great_blue_heron
    actions: [discord, email, webhook]
  - match:
      class: bird
    actions: [discord]
  - match:
      class: human
    actions: [log]
  - match:
      camera: pond-south
      class: raccoon
    actions: [discord, webhook]

notifications:
  discord:
    enabled: true
    webhook_url: "https://discord.com/api/webhooks/..."
    mention_role: ""
    include_snapshot: true
  email:
    enabled: false
    smtp_host: ""
    smtp_port: 587
    smtp_user: ""
    smtp_pass: ""
    to_addresses: []
    include_snapshot: true
  webhooks:
    - name: valve-controller
      enabled: false
      url: "http://192.168.1.x/api/fire"
      method: POST
      headers:
        Authorization: "Bearer YOUR_TOKEN"
      include_snapshot_url: true
    - name: home-assistant
      enabled: false
      url: "http://homeassistant.local:8123/api/webhook/scarguard"
      method: POST
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

## RTSP Notes

- UniFi Protect RTSP must be enabled per-camera in Protect UI on the UDM
- RTSP URL format: `rtsp://172.16.0.1:7447/<stream_token>`
- Use 720p substream for inference — 4K wastes GPU cycles
- OpenCV `VideoCapture` handles RTSP natively; set `cv2.CAP_PROP_BUFFERSIZE` to 1 to reduce frame lag
- Camera models: G3 Flex and G5 Flex (G3 may be replaced with another G5)

## Arm/Disarm Modes

ScarGuard supports three operating modes controlled by `system.armed` and the optional `system.schedule` section:

- **Always armed (default):** Set `armed: true` and omit the `schedule` section. The system monitors continuously. Use the dashboard button to toggle manually at any time.
- **Always off:** Set `armed: false` and omit the `schedule` section. Camera threads still run but detections are not processed. Toggle via the dashboard when needed.
- **Scheduled:** Include a `schedule` section with `enabled: true` and `arm_time`/`disarm_time` (or `use_solar: true` with latitude/longitude). The system arms and disarms automatically at the configured times. Manual toggles from the dashboard override the schedule until the next scheduled transition. Set `enabled: false` to temporarily disable the schedule without clearing your configured times.

The schedule is entirely optional. If the `schedule` key is missing, `enabled` is false, or both time fields are empty, no automatic transitions occur and the armed state is under manual control only.

## Service Communication

- **Between services:** Redis pub/sub. Detector publishes detection events; notifier and web UI subscribe.
- **Config:** All services read from mounted `config/scarguard.yml` in external data directory. Web UI can write to it. Detector and notifier auto-restart on config file changes.
- **Database:** SQLite at `data/scarguard.db`, shared volume between web and detector.
