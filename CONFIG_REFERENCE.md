# ScarGuard — Config & Detection Reference

## Config File Format (scarguard.yml)

```yaml
system:
  armed: true
  log_level: info
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
   - Save to SQLite (timestamp, class, confidence, camera, snapshot path)
   - Publish to Redis pub/sub channel `scarguard:detections`
   - Save annotated snapshot frame to disk
6. Notifier picks up events from Redis and dispatches to configured channels
7. Web UI subscribes to Redis for live event feed via SSE

## RTSP Notes

- UniFi Protect RTSP must be enabled per-camera in Protect UI on the UDM
- RTSP URL format: `rtsp://172.16.0.1:7447/<stream_token>`
- Use 720p substream for inference — 4K wastes GPU cycles
- OpenCV `VideoCapture` handles RTSP natively; set `cv2.CAP_PROP_BUFFERSIZE` to 1 to reduce frame lag
- Camera models: G3 Flex and G5 Flex (G3 may be replaced with another G5)

## Service Communication

- **Between services:** Redis pub/sub. Detector publishes detection events; notifier and web UI subscribe.
- **Config:** All services read from mounted `config/scarguard.yml` in external data directory. Web UI can write to it. Detector and notifier auto-restart on config file changes.
- **Database:** SQLite at `data/scarguard.db`, shared volume between web and detector.
