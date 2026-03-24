# ScarGuard — Config & Detection Reference

## Config file format (`scarguard.yml`)

```yaml
system:
  armed: true
  log_level: info
  timezone: UTC
  snapshot_retention_days: 30

web:
  http_port: 8080
  https_port: 8443
  ssl:
    enabled: false
    cert_path: /config/certs/scarguard.crt
    key_path: /config/certs/scarguard.key

cameras:
  - name: pond-north
    rtsp_url: "rtsp://172.16.0.1:7447/STREAM_TOKEN_1"
    enabled: true
    resolution: 720
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

redis:
  host: redis
  port: 6379
```

## Currently enforced by code

Only keys read by these runtime/config-validation paths are listed here:

- `services/detector/src/main.py`
- `services/notifier/src/main.py`
- `services/web/src/config_model.py`

### `system`
- `system.armed`
- `system.log_level`
- `system.timezone`

### `cameras[]`
- `cameras[].name`
- `cameras[].rtsp_url`
- `cameras[].enabled`
- `cameras[].resolution`

### `detection`
- `detection.model_path`
- `detection.confidence_threshold`
- `detection.target_classes`
- `detection.cooldown_seconds`
- `detection.frame_skip`

### `notifications`
- `notifications.discord.enabled`
- `notifications.discord.webhook_url`
- `notifications.discord.mention_role`
- `notifications.discord.include_snapshot`
- `notifications.email.enabled`
- `notifications.email.smtp_host`
- `notifications.email.smtp_port`
- `notifications.email.smtp_user`
- `notifications.email.smtp_pass`
- `notifications.email.to_addresses`
- `notifications.email.include_snapshot`

### `redis`
- `redis.host`
- `redis.port`

## Planned / not yet enforced

- `web.ssl.*` — **status: planned**
- `snapshot_retention_days` — **status: planned**
- `cameras[].exclusion_zones` runtime behavior — **status: UI-preserved only**
- `action_rules` — **status: not implemented in runtime**
- `notifications.webhooks` — **status: not implemented in runtime**

## Structured config save behavior

`services/web/src/routes/config.py` preserves unknown keys when saving structured config by merging edited sections into the existing document (including top-level unknown keys and unknown per-camera fields). This means unsupported keys are retained even when not enforced by runtime services.

## Detection logic

1. Pull frames from each RTSP stream (OpenCV `VideoCapture`)
2. Run YOLO inference on GPU (`model.predict()`)
3. Filter results by target classes and confidence threshold
4. Apply cooldown dedup (avoid repeated events for the same standing object)
5. On new detection event:
   - Save to SQLite (timestamp, class, confidence, camera, snapshot path)
   - Publish to Redis pub/sub channel `scarguard:detections`
   - Save annotated snapshot frame to disk
6. Notifier picks up events from Redis and dispatches to configured channels
7. Web UI subscribes to Redis for live event feed via SSE

## RTSP notes

- UniFi Protect RTSP must be enabled per-camera in Protect UI on the UDM
- RTSP URL format: `rtsp://172.16.0.1:7447/<stream_token>`
- Use 720p substream for inference — 4K wastes GPU cycles
- OpenCV `VideoCapture` handles RTSP natively; set `cv2.CAP_PROP_BUFFERSIZE` to 1 to reduce frame lag

## Service communication

- **Between services:** Redis pub/sub. Detector publishes detection events; notifier and web UI subscribe.
- **Config:** All services read from mounted `config/scarguard.yml` in external data directory. Web UI can write to it. Detector and notifier auto-restart on config file changes.
- **Database:** SQLite at `data/scarguard.db`, shared volume between web and detector.
