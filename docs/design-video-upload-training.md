# Design: Video-Upload Training Pipeline + Web UI Training

## 1. Overview

Two features in one branch, sharing the pause/resume infrastructure:

**Feature A — Video-as-training-source.** Upload short video clips (UniFi
exports, phone recordings), run low-confidence YOLO inference across every
frame, surface candidate detections in a labeling queue, feed approved labels
into the training dataset as a fourth source alongside Orin DB / Roboflow /
Open Images.

**Feature B — Web UI training.** Kick off `prepare_dataset.py` →
`train.py` from the admin UI.  The detector pauses inference in-process,
yielding the GPU to the trainer.  On completion (or failure), the detector
resumes automatically.

Both features build on the existing training pipeline — `prepare_dataset.py`
and `train.py` remain CLI-runnable with no behavioral changes.

---

## 2. Architecture

```
┌──────────────┐     POST /admin/training/uploads
│   Web (UI)   │──────────────────────────────────────┐
│   FastAPI     │     POST /admin/training/jobs        │
│              │──────────────────────────────────┐    │
└──────┬───────┘                                  │    │
       │ SSE progress                             │    │
       │ (Redis keys)                             ▼    ▼
       │                              ┌────────────────────┐
       │                              │  Redis              │
       │                              │  - job queue        │
       │                              │  - pause/resume     │
       │                              │  - progress keys    │
       │                              │  - heartbeat        │
       │                              └────┬───────────────┘
       │                                   │
  ┌────┴──────────┐          ┌─────────────┴──────────────┐
  │  Detector     │  pause   │  Trainer                   │
  │  (live RTSP)  │◄────────►│  (video processing,        │
  │               │  resume  │   prepare_dataset, train)   │
  │  camera → GPU │          │  GPU (when detector paused) │
  └───────────────┘          └─────────────────────────────┘
          │                              │
          ▼                              ▼
  ┌───────────────────────────────────────────────────┐
  │  Shared Volumes                                   │
  │  /data    — scarguard.db, snapshots, training_uploads │
  │  /models  — .pt / .engine files                   │
  │  /config  — scarguard.yml                         │
  └───────────────────────────────────────────────────┘
```

---

## 3. Data Model

All new tables live in the existing `scarguard.db` (WAL mode).  Writer
contention is negligible: the trainer writes training events only while the
detector is paused (no competing detection writes), and upload/job status
updates are infrequent single-row UPDATEs.

### 3.1 `training_uploads`

```sql
CREATE TABLE IF NOT EXISTS training_uploads (
    id                TEXT    PRIMARY KEY,   -- UUID4 hex
    filename          TEXT    NOT NULL,      -- original upload filename
    target_class_hint TEXT,                  -- 'duck' | 'heron' | 'raccoon' | 'background' | NULL
    frame_count       INTEGER,              -- populated after extraction
    detection_count   INTEGER,              -- populated after processing (post-dedupe)
    status            TEXT    NOT NULL DEFAULT 'uploaded',
        -- uploaded → processing → processed → failed
    error             TEXT,                  -- error message if failed
    created_at        TEXT    NOT NULL,      -- ISO 8601
    processed_at      TEXT                   -- ISO 8601
);

CREATE INDEX IF NOT EXISTS idx_tu_status ON training_uploads(status);
```

### 3.2 `training_events`

```sql
CREATE TABLE IF NOT EXISTS training_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id         TEXT    NOT NULL REFERENCES training_uploads(id) ON DELETE CASCADE,
    frame_idx         INTEGER NOT NULL,
    timestamp_in_video REAL,                -- seconds into the video
    bbox              TEXT    NOT NULL,      -- JSON [x_center, y_center, width, height] normalized 0-1
    predicted_class   TEXT    NOT NULL,
    confidence        REAL    NOT NULL,
    target_class_hint TEXT,                  -- inherited from upload record
    detection_pass    TEXT    NOT NULL,      -- 'low' | 'normal'
    review_state      TEXT    NOT NULL DEFAULT 'pending',
        -- pending | approved | rejected | corrected
    corrected_class   TEXT,                  -- set when review_state = 'corrected'
    created_at        TEXT    NOT NULL,
    reviewed_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_te_upload    ON training_events(upload_id);
CREATE INDEX IF NOT EXISTS idx_te_review    ON training_events(review_state);
CREATE INDEX IF NOT EXISTS idx_te_pass      ON training_events(pass);
CREATE INDEX IF NOT EXISTS idx_te_frame     ON training_events(upload_id, frame_idx);
```

### 3.3 `training_jobs`

```sql
CREATE TABLE IF NOT EXISTS training_jobs (
    id            TEXT    PRIMARY KEY,       -- UUID4 hex
    type          TEXT    NOT NULL,          -- 'process_video' | 'prepare_dataset' | 'train' | 'prepare_and_train'
    params        TEXT    NOT NULL,          -- JSON (upload_ids, overrides, etc.)
    status        TEXT    NOT NULL DEFAULT 'queued',
        -- queued | running | completed | failed | cancelled
    result        TEXT,                      -- JSON (metrics, output path, dataset stats, error detail)
    created_at    TEXT    NOT NULL,
    started_at    TEXT,
    completed_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_tj_status ON training_jobs(status);
```

The web service creates these tables on startup (same pattern as existing
`detection_events` table creation in the web `db.py` module).

### 3.4 Storage layout

```
/data/
├── scarguard.db                          # existing + new tables
├── snapshots/                            # existing live snapshots
└── training_uploads/
    └── <upload_id>/
        ├── original.mp4                  # uploaded video (retained until delete)
        └── frames/
            ├── 000000.jpg
            ├── 000001.jpg
            └── ...
```

---

## 4. Pause/Resume Mechanism

### 4.1 Choice: Redis pub/sub command + key-based ack

**Why Redis, not DB flag or socket:**
- Redis pub/sub is already the IPC pattern for eval requests, snapshot
  requests, and model introspection RPCs.
- Pub/sub delivers instantly (no polling delay).
- Confirmation via Redis key matches the eval progress/result pattern.
- No new infrastructure or dependencies.

### 4.2 Protocol

**Redis channels and keys:**

| Name | Type | Direction | Purpose |
|------|------|-----------|---------|
| `scarguard:detector:command` | pub/sub channel | trainer → detector | Pause/resume commands |
| `scarguard:detector:state` | key (JSON, TTL 1h) | detector → trainer/web | Current state |
| `scarguard:trainer:heartbeat` | key (TTL 90s) | trainer → detector | Liveness signal |

**Pause sequence:**

```
Trainer                         Detector
   │                               │
   │─── PUBLISH command ──────────►│  {"action":"pause", "request_id":"abc", "timeout":86400}
   │    (scarguard:detector:command)│
   │                               │  1. Set paused_ref = True
   │                               │  2. Wait for in-flight inference to drain (≤5s)
   │                               │  3. Unload all models from ModelPool
   │                               │  4. torch.cuda.empty_cache()
   │                               │  5. Start heartbeat watcher thread
   │                               │
   │◄── SET state ─────────────────│  {"state":"paused", "since":"...", "request_id":"abc"}
   │    (scarguard:detector:state) │
   │                               │
   │─── SET heartbeat (every 30s)─►│  "alive" (TTL 90s)
   │    (scarguard:trainer:heartbeat)
   │                               │
   │    ... training work ...      │
   │                               │
   │─── PUBLISH command ──────────►│  {"action":"resume", "request_id":"abc"}
   │                               │  1. Reload models (potentially new .pt from training)
   │                               │  2. Set paused_ref = False
   │                               │  3. Stop heartbeat watcher
   │                               │
   │◄── SET state ─────────────────│  {"state":"running", "since":"..."}
```

**Drain logic:** After setting `paused_ref`, the detector waits up to 5
seconds for all FairLock instances in the ModelPool to become idle.  Since
`paused_ref` prevents new `predict()` calls from entering the lock, any
in-flight inference finishes within one frame (~50–100ms on Orin).  The 5s
timeout is generous.

### 4.3 Detector changes

Add to `services/detector/src/main.py`:

1. **`paused_ref: AtomicRef[bool]`** — checked in `run_camera()` before
   `detector.predict()`, same position as the existing `armed_ref` check.
   When True, the camera thread skips inference and sleeps 1 second (drops
   frames; this is a maintenance window).

2. **`_command_listener` thread** — subscribes to
   `scarguard:detector:command`, calls `_handle_pause()` /
   `_handle_resume()` on the ModelPool and AtomicRef.

3. **Heartbeat watcher** — when paused, polls
   `scarguard:trainer:heartbeat` every 30s.  Auto-resumes if the key
   is missing (trainer died) OR if `timeout` seconds have elapsed since
   pause began.

No changes to FairLock, RTSPStream, EventProcessor, or the detection
pipeline logic.

### 4.4 Safety guarantees

| Failure mode | Recovery |
|---|---|
| Training job fails (exception) | Trainer sends resume in `finally` block |
| Trainer container crashes | Heartbeat expires (90s); detector auto-resumes |
| Trainer container killed (SIGKILL) | Same — heartbeat TTL expires |
| Pause timeout exceeded | Detector auto-resumes after `timeout` seconds |
| Detector restarts while paused | Starts unpaused (default state) |
| Redis connection lost during pause | Detector can't read heartbeat → auto-resume after timeout |
| OOM during training | Kernel kills trainer → heartbeat expires → auto-resume |

**Hard requirement:** the detector must never remain paused indefinitely.
The timeout (default 86400s = 24h) is the backstop for a trainer that is alive but never finishes; the 90s heartbeat TTL is the crash guard.

### 4.5 Fallback: container stop/start

If in-process pause proves fragile (model unload doesn't reliably free GPU
memory, or camera threads misbehave), the fallback is:

1. Trainer calls `docker stop scarguard-detector` (via Docker socket proxy
   with `POST` permission on `/containers/*/stop`).
2. Training runs.
3. Trainer calls `docker start scarguard-detector`.

This requires adding `POST` capability to the existing `docker-socket-proxy`
service (currently read-only: `CONTAINERS=1`, `EVENTS=1`).  Flag this in PR
review if we reach for it.

---

## 5. Feature A: Video Ingestion

### 5.1 Upload flow

```
User                Web Service              Disk
 │                       │                     │
 │── POST /admin/training/uploads ──►│         │
 │   (multipart: file + hint)       │         │
 │                       │── write ─────────►  /data/training_uploads/<id>/original.mp4
 │                       │── INSERT ──►  training_uploads (status='uploaded')
 │◄── 201 {id, status} ─│                     │
```

- **Max duration: 1 minute.**  The upload handler probes the video with
  OpenCV (`CAP_PROP_FRAME_COUNT / CAP_PROP_FPS`) and rejects anything over
  60 seconds.  At 30 fps that's ≤ 1,800 frames (~180 MB of JPEGs) — bounded
  and manageable on the Orin's storage.
- Max upload file size: configurable via `TRAINING_UPLOAD_MAX_BYTES` env var
  (default 500 MB).  Chunked upload using the same pattern as model uploads
  (`routes/models.py`).
- Accepted formats: `.mp4`, `.avi`, `.mkv`, `.mov` (validated by extension +
  OpenCV probe on first frame).
- `target_class_hint`: form dropdown — duck / heron / raccoon / background.

### 5.2 Video processing

Video processing runs in the **trainer** container as a `process_video` job.
It requires GPU access, so the detector is paused first.

**Model access decision: load a fresh instance after the detector frees
GPU.**

Why not borrow the detector's loaded model: the trainer and detector are
separate containers (separate processes, separate CUDA contexts).  Object
references can't cross process boundaries.  Instead, the trainer loads the
same model path from `/models/` after the detector unloads and frees GPU
memory.  This mirrors how the evaluator already works (loads its own YOLO
instances, deletes them afterward).

**Processing steps:**

1. Pause detector (wait for ack).
2. Load YOLO model from `detection.model_path` in config.
3. Open video with OpenCV `VideoCapture`.
4. For each frame:
   a. Extract frame, save as JPEG to
      `/data/training_uploads/<id>/frames/<frame_idx>.jpg`.
   b. Run inference at `conf=0.05` (low-confidence pass).
   c. Collect all detections with bbox, class, confidence.
   d. Tag each detection: `pass='normal'` if confidence ≥ config threshold,
      else `pass='low'`.
   e. Publish progress every 100 frames.
5. Run dedupe (section 5.3) across all collected detections.
6. INSERT surviving detections into `training_events`.
7. Update `training_uploads.status = 'processed'`, set `frame_count`,
   `detection_count`.
8. Unload model, `torch.cuda.empty_cache()`.
9. Resume detector.

For **batch processing** (multiple pending uploads in one job): the trainer
pauses once, processes all uploads sequentially, resumes once.  The
`process_video` job accepts a list of upload IDs.

### 5.3 Two-pass confidence tagging

There is a single inference pass per frame at `conf=0.05`.  The "two-pass"
label is a post-hoc classification:

- **`pass='normal'`** — confidence ≥ the model's configured threshold
  (from `detection.confidence_threshold` in scarguard.yml).  These are
  detections the live system would have caught.
- **`pass='low'`** — confidence between 0.05 and the normal threshold.
  These are the high-value training samples the model is uncertain about.

The labeling UI exposes a filter/sort by pass so the user can prioritize
reviewing "hard" detections.

### 5.4 Dedupe algorithm

After collecting all raw detections across all frames:

```
For each class independently:
  Sort detections by frame_idx ascending.
  Walk forward through the sorted list.
  For each detection D:
    Look back at most N frames (configurable, default 5).
    If any prior detection of the same class has IoU > threshold
    (configurable, default 0.85):
      Keep whichever has higher confidence; drop the other.
```

**Configurable parameters** (defaults in `training.video` config section):

| Parameter | Config key | Default |
|---|---|---|
| Low-confidence threshold | `training.video.low_confidence` | 0.05 |
| Dedupe IoU threshold | `training.video.dedupe_iou` | 0.85 |
| Dedupe frame window | `training.video.dedupe_window` | 5 |
| Max video duration | `training.video.max_duration_seconds` | 60 |
| Background sample interval | `training.video.background_sample_interval` | 10 |

These can also be overridden per-job in the POST body.

### 5.5 Labeling queue

The labeling queue is a web UI for reviewing `training_events` records.

**View:** Renders one detection at a time — frame image with bbox overlay,
predicted class and confidence, pass (easy/hard badge), target class hint.

**Actions (one keystroke each):**

| Key | Action | Effect |
|---|---|---|
| `a` | Approve | `review_state = 'approved'`, keep predicted_class |
| `x` | Reject | `review_state = 'rejected'` |
| `c` | Correct | `review_state = 'corrected'`, `corrected_class` = hint (or open picker if no hint / hint matches prediction) |
| `j` / `→` | Next | Advance to next pending detection |
| `k` / `←` | Previous | Go back |

**Filters:** upload, class, pass (low/normal), review state.  Default view:
pending detections sorted by confidence descending within each upload.

**Bulk operations:**

- "Approve All Normal" — approves all `pass='normal'` detections in an
  upload (the model was confident; these are likely correct).
- "Reject All" — rejects all pending detections in an upload.

### 5.6 Background uploads

When `target_class_hint = 'background'`:

- Processing still runs inference (to surface any unexpected detections for
  the user to review/reject).
- On export, frames are **sampled at a configurable interval** (default:
  every 10th frame) and emitted as background samples (image + empty label
  file).  A 1-minute 30 fps video produces ~180 background samples instead
  of 1,800, keeping the dataset balanced against positive classes.
- The sample interval is configurable via
  `training.video.background_sample_interval` (default `10`) and can be
  overridden per-job.
- The labeling UI shows a banner: "Background upload — sampled frames will
  be exported as negative samples.  Review detections below to reject false
  alarms."

---

## 6. Feature B: Web UI Training

### 6.1 Job queue

**Storage:** Job metadata in SQLite (`training_jobs` table) for durability.
Real-time progress in Redis keys for SSE streaming.

**Flow:**

1. Web UI `POST /admin/training/jobs` → validates params, INSERTs job row
   (`status='queued'`), publishes notification to Redis channel
   `scarguard:training:job:notify`.
2. Trainer listens on `scarguard:training:job:notify`.  On notification,
   queries `training_jobs` for oldest `status='queued'` row (poll as
   fallback every 30s in case pub/sub message was missed).
3. Trainer sets `status='running'`, `started_at`, publishes real-time
   progress to `scarguard:training:job:<id>:progress` (Redis key, TTL 5min,
   refreshed on each update).
4. On completion: sets `status='completed'`, `result` JSON, `completed_at`.
5. On failure: sets `status='failed'`, `result` JSON with error detail.

**Redis keys for real-time progress:**

| Key | Content | TTL |
|---|---|---|
| `scarguard:training:job:<id>:progress` | JSON `{phase, pct, detail, epoch, mAP}` | 300s (refreshed) |
| `scarguard:training:job:<id>:log` | Redis list (capped at 500 lines) | 3600s |

### 6.2 Job types

| Type | GPU needed | Pauses detector | Description |
|---|---|---|---|
| `process_video` | Yes | Yes | Extract frames + inference on uploaded video(s) |
| `prepare_dataset` | No | No | Merge sources → YOLO dataset |
| `train` | Yes | Yes | Fine-tune YOLO model |
| `prepare_and_train` | Yes | Yes (train phase only) | Chained: prepare then train |

**`process_video` params:**
```json
{
  "upload_ids": ["uuid1", "uuid2"],
  "low_confidence": 0.05,
  "dedupe_iou": 0.85,
  "dedupe_window": 5
}
```

**`prepare_dataset` params:**
```json
{
  "skip_orin": false,
  "skip_roboflow": false,
  "skip_oid": false,
  "skip_training_uploads": false,
  "max_oid_per_class": 1500,
  "val_split": 0.15,
  "seed": 42
}
```

**`train` params:**
```json
{
  "base_model": "yolov8n.pt",
  "epochs": 100,
  "batch_size": 2,
  "image_size": 480,
  "patience": 20,
  "output_name": "pond_v3.pt",
  "force": false
}
```

**`prepare_and_train` params:** union of `prepare_dataset` + `train` params.

Parameter defaults come from `training.defaults` in scarguard.yml.  Job
params override.

### 6.3 Trainer service

New `services/trainer/` directory:

```
services/trainer/
├── Dockerfile
├── entrypoint.sh
├── requirements.txt
└── src/
    ├── main.py           # Job consumer loop
    ├── video_processor.py # Frame extraction + inference + dedupe
    ├── job_runner.py      # Dispatches job types
    ├── pause_client.py    # Pause/resume/heartbeat protocol
    └── dataset_export.py  # Read training_events → Sample list for prepare_dataset
```

**Dockerfile:** Same base image as detector
(`dustynv/l4t-pytorch:r36.4.0`).  Additional pip packages: `roboflow`,
`fiftyone` (for Open Images, if used), `opencv-python-headless`.

**Idle behavior:** The main loop blocks on Redis `SUBSCRIBE` +
`training_jobs` polling.  No CUDA context is created until a GPU job starts.
Idle memory footprint: ~50MB.

**OOM protection:** `deploy.resources.limits.memory: 6g` in
docker-compose.  If training OOMs, the kernel kills the trainer process.
The container restarts (restart policy), but the detector's heartbeat
watcher has already auto-resumed.

### 6.4 `scarguard.yml` additions

New `training` section alongside existing top-level sections:

```yaml
training:
  sources:
    roboflow:
      api_key: ""                           # SENSITIVE — encrypted at rest
      datasets:
        - workspace: "louis-berndroth2-gmail-com"
          project: "heron-detection"
        - workspace: "harbin-institute-of-technology-hpsg8"
          project: "raccon-3osqx"
    open_images:
      max_per_class: 1500
      workers: 16
    local:
      enabled: true                         # Include detection_events feedback
    training_uploads:
      enabled: true                         # Include video upload annotations

  defaults:
    base_model: "yolov8n.pt"
    epochs: 100
    batch_size: 2
    image_size: 480
    patience: 20
    val_split: 0.15

  video:
    low_confidence: 0.05
    dedupe_iou: 0.85
    dedupe_window: 5
    max_duration_seconds: 60
    background_sample_interval: 10    # export every Nth frame from background uploads
```

### 6.5 Credential management

`training.sources.roboflow.api_key` is a secret.  It follows the existing
credential pattern:

- **Encryption at rest:** `secret_box.py` encrypts the value in
  `scarguard.yml` (prefix `enc:v1:`), same as Tuya API keys.
- **Redaction in UI:** `config_redact.py` adds
  `training.sources.roboflow.api_key` to the sensitive paths list.
  Viewers see `***REDACTED***`.
- **Reveal via admin:** The existing `/config/secrets` endpoint includes
  the training key.  The config form's "Reveal Secrets" button works as-is.
- **Fail fast:** When a job requires Roboflow data and the key is empty or
  missing, the job fails immediately with error `"Roboflow API key not
  configured — set training.sources.roboflow.api_key in scarguard.yml"`.

---

## 7. Container Topology

### 7.1 docker-compose.yml addition

```yaml
  trainer:
    build:
      context: .
      dockerfile: services/trainer/Dockerfile
    container_name: scarguard-trainer
    restart: unless-stopped
    user: "0:0"                              # entrypoint drops to non-root via gosu
    volumes:
      - scarguard-data:/data
      - scarguard-models:/models
      - scarguard-config:/config:ro
    environment:
      - CONFIG_PATH=/config/scarguard.yml
      - DB_PATH=/data/scarguard.db
      - SNAPSHOT_DIR=/data/snapshots
      - MODELS_DIR=/models
      - TRAINING_UPLOADS_DIR=/data/training_uploads
    depends_on:
      - redis
    cap_drop:
      - ALL
    cap_add:
      - CHOWN
      - FOWNER
      - DAC_OVERRIDE
      - SETUID
      - SETGID
    healthcheck:
      test: ["CMD", "test", "-f", "/tmp/healthy"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
```

### 7.2 docker-compose.gpu.yml addition

```yaml
  trainer:
    runtime: nvidia
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
      - NVIDIA_DRIVER_CAPABILITIES=compute,utility,video
    deploy:
      resources:
        limits:
          memory: 6g
```

---

## 8. API Endpoints

### 8.1 Video uploads

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/admin/training/uploads` | viewer+ | Uploads list page |
| `POST` | `/admin/training/uploads` | admin | Upload video (multipart) |
| `GET` | `/admin/training/uploads/{id}` | viewer+ | Upload detail + labeling queue |
| `DELETE` | `/admin/training/uploads/{id}` | admin | Delete upload + frames + events |
| `GET` | `/admin/training/uploads/{id}/frames/{idx}` | viewer+ | Serve frame JPEG |

### 8.2 Labeling

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/admin/training/uploads/{id}/events/{eid}/review` | admin | Set review_state |
| `POST` | `/admin/training/uploads/{id}/bulk-review` | admin | Bulk approve/reject |

**Review body:** `{"action": "approved" | "rejected" | "corrected", "corrected_class": "..."}`

**Bulk review body:** `{"action": "approved" | "rejected", "filter": {"pass": "normal"}}`

### 8.3 Training jobs

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/admin/training/jobs` | viewer+ | Jobs list page |
| `POST` | `/admin/training/jobs` | admin | Submit new job |
| `GET` | `/admin/training/jobs/{id}` | viewer+ | Job detail page |
| `GET` | `/admin/training/jobs/{id}/stream` | viewer+ | SSE progress stream |
| `POST` | `/admin/training/jobs/{id}/cancel` | admin | Cancel queued/running job |

**Submit body:**
```json
{
  "type": "process_video",
  "params": { ... }
}
```

Returns `201` with `{"id": "...", "status": "queued"}`.

**Cancel behavior:** For queued jobs, sets `status='cancelled'`.  For
running jobs, sets a Redis key `scarguard:training:job:<id>:cancel` that the
trainer polls periodically.  The trainer aborts at the next checkpoint
(between epochs or between frames) and resumes the detector.

### 8.4 Training sources config

No new dedicated endpoints.  The existing `/config` page and
`POST /config` handler already support arbitrary YAML sections.  The
`training` section is handled by:

1. Adding `training.sources.roboflow.api_key` to the sensitive paths in
   `config_redact.py`.
2. Adding a "Training" sub-tab to the config form (alongside System,
   Detection, Cameras, etc.) that renders fields for the `training` section.

### 8.5 Detector state (informational)

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/admin/training/detector-state` | viewer+ | Returns current detector state from Redis |

Returns JSON: `{"state": "running" | "paused", "since": "...", "request_id": "..."}`.
The training jobs panel uses this to show detector status.

---

## 9. `prepare_dataset.py` Integration

### 9.1 New CLI flags

```
--local-db PATH              Read detection_events directly from SQLite
                             (replaces SSH pull when running on the Orin)
--local-snapshots PATH       Snapshot directory for local DB mode
--training-uploads-db PATH   Read approved training_events from SQLite
--training-uploads-frames PATH  Frame JPEG directory for training uploads
--skip-training-uploads      Skip training uploads source
```

The SSH path (`--orin-host`) remains the default for manual CLI use from a
dev machine.  The trainer invokes with `--local-db` and
`--training-uploads-db` since it reads directly from the mounted volumes.

### 9.2 New source function

```python
def pull_training_uploads(
    db_path: str,
    frames_dir: str,
    work_dir: Path,
) -> list[Sample]:
    """Fourth source: approved annotations from video uploads."""
```

**Logic:**

1. Query `training_events` joined with `training_uploads` where
   `review_state IN ('approved', 'corrected')`.
2. Group by `(upload_id, frame_idx)` to collect multiple detections per
   frame into one label file.
3. For each group: build YOLO label lines using `corrected_class` (if
   corrected) or `predicted_class`, resolve class to `UNIFIED_CLASSES`
   index via existing `_resolve_class()`.
4. For background uploads: emit every Nth frame as a background sample
   (empty label), where N = `background_sample_interval` (default 10).
5. Return `list[Sample]` with `source="training_uploads"`.

### 9.3 Trainer invocation

The trainer builds the `prepare_dataset.py` command from job params +
`scarguard.yml`:

```bash
python prepare_dataset.py \
    --local-db /data/scarguard.db \
    --local-snapshots /data/snapshots \
    --training-uploads-db /data/scarguard.db \
    --training-uploads-frames /data/training_uploads \
    --roboflow-key <from training.sources.roboflow.api_key> \
    --output /data/training_workspace/merged_dataset \
    --max-oid-per-class 1500 \
    --val-split 0.15 \
    --seed 42
```

The scripts are invoked as **subprocesses** (`subprocess.Popen`), preserving
them as standalone CLI tools.  stdout/stderr are streamed to the job log
(Redis list + written to `/data/training_workspace/logs/<job_id>.log`).

---

## 10. Frontend

### 10.1 Admin navigation

Add to the admin nav (alongside existing Training, Evaluate links):

- **Uploads** → `/admin/training/uploads`
- **Jobs** → `/admin/training/jobs`

### 10.2 Upload page (`/admin/training/uploads`)

**Layout:**

- Upload form at top: file input + target class hint dropdown + submit
  button.
- Table below: all uploads with columns — filename, hint, frame count,
  detection count, status, created, actions (Process / Label / Delete).
- "Process All Pending" button submits a `process_video` job for all
  `status='uploaded'` entries.

### 10.3 Labeling queue (`/admin/training/uploads/{id}`)

**Layout:**

- Left panel (60%): frame image with bbox overlay (CSS-positioned div,
  same pattern as existing feedback page).
- Right panel (40%): detection metadata (class, confidence, pass badge),
  review controls, upload-level stats (N pending / N approved / N rejected).
- Bottom strip: keyboard shortcut legend.

**Interactivity:**

- HTMX for review actions (no full page reload).  `POST` to review
  endpoint returns updated detection card.
- Auto-advance to next pending detection after review action.
- Class correction: inline `<datalist>` dropdown pre-filled with target
  classes from config, defaulting to hint.

### 10.4 Training jobs panel (`/admin/training/jobs`)

**Layout:**

- "New Job" dropdown button: Process Videos / Prepare Dataset / Train /
  Prepare & Train.
- Active job card (if running): progress bar, phase label, epoch counter
  (for train jobs), log tail (scrolling `<pre>` block, HTMX-polled or SSE).
- Detector state badge: "Running" (green) / "Paused" (amber).
- Job history table: type, status, created, duration, result summary.

**Train job form:** Pre-filled from `training.defaults` config.  Fields:
base model (dropdown from `/models/`), epochs, batch size, image size,
patience, output name.

### 10.5 Training sources settings

New "Training" sub-tab in the config page (`/config`):

- Roboflow API key (password input, revealable).
- Roboflow datasets list (add/remove rows: workspace + project).
- Open Images: max per class, workers.
- Local sources: enabled toggles for detection feedback and training
  uploads.
- Default training params: base model, epochs, batch size, image size,
  patience, val split.
- Video processing params: low confidence, dedupe IoU, dedupe window.

Follows the existing config form pattern — fields organized in
`.card` / `.field-group` / `.field-row` divs, expert-mode toggle for
advanced params.

---

## 11. Safety & Recovery

### 11.1 Resume-on-failure

```python
async def run_gpu_job(job, pause_client, ...):
    await pause_client.pause(timeout=job.timeout)
    try:
        await do_work(job)
    finally:
        await pause_client.resume()
```

The `finally` block is the primary safety net.  Even if `do_work()` raises,
OOMs, or is interrupted, resume is called.

### 11.2 Heartbeat

While the detector is paused, the trainer writes
`scarguard:trainer:heartbeat` every 30s (TTL 90s).  The detector's heartbeat
watcher checks every 30s.  If the key is absent, the trainer is assumed dead
and the detector auto-resumes.

### 11.3 Pause timeout

The pause request includes `timeout` (default 86400s = 24h).  The detector tracks
`paused_since` and auto-resumes when `now - paused_since > timeout`.
This covers: trainer hung, heartbeat mechanism itself broken, Redis down.

### 11.4 OOM during training

Docker memory limit (`6g`) causes the trainer to be OOM-killed.  The
container restarts (restart policy `unless-stopped`), but the current job is
lost.  The detector's heartbeat expires within 90s → auto-resume.

The trainer's `main.py` startup checks for jobs with `status='running'`
and marks them `status='failed'` with `result={"error": "Trainer
restarted — job interrupted (possible OOM)"}`.

### 11.5 Missing credentials fail-fast

Before starting a job that requires Roboflow or Open Images, the trainer
reads `training.sources` from `scarguard.yml`.  If `roboflow.api_key` is
empty and the job doesn't skip Roboflow, the job fails immediately:

```json
{
  "status": "failed",
  "result": {
    "error": "Roboflow API key not configured. Set training.sources.roboflow.api_key in Settings → Training."
  }
}
```

No pause is requested for fast failures.  The detector is never interrupted.

### 11.6 Concurrent job prevention

Only one job runs at a time.  The trainer holds an in-process lock.  If a
second job is submitted while one is running, it stays queued.  The web UI
shows a "Job in progress" indicator and disables the submit button.

---

## 12. Test Plan

| Area | Test | Type |
|---|---|---|
| Dedupe correctness | Same class, IoU > 0.85 within window → keep highest conf | Unit |
| Dedupe edge cases | Different classes same bbox not deduped; frame gap > window not deduped | Unit |
| Two-pass classification | Detections above/below threshold tagged correctly | Unit |
| training_events → YOLO export | Approved events produce correct label lines; corrected class used; multiple detections per frame merged; background uploads emit empty labels | Unit |
| Job queue state transitions | queued → running → completed; queued → cancelled; running → failed | Unit |
| Pause/resume happy path | Detector acks pause, state key set, camera threads skip inference, resume restores inference | Integration |
| Pause with training failure | Exception during training → detector resumes (finally block) | Integration |
| Pause timeout | Detector auto-resumes after timeout expires | Integration |
| Heartbeat recovery | Trainer heartbeat stops → detector auto-resumes within 2 check cycles | Integration |
| Missing credentials | Job with empty Roboflow key fails fast, detector not paused | Unit |
| Video upload validation | Rejects non-video files, respects max size | Unit |
| Bulk review | "Approve All Normal" updates only pass='normal' pending events | Unit |
| prepare_dataset.py `--local-db` | Reads detection_events directly, produces same output as SSH path | Integration |
| prepare_dataset.py training uploads source | Reads training_events, emits correct Samples | Integration |

---

## 13. Commit Plan

Atomic commits per logical chunk, in implementation order:

| # | Scope | Description |
|---|---|---|
| 1 | Schema | Migration: create `training_uploads`, `training_events`, `training_jobs` tables |
| 2 | Detector | Pause/resume hook: `paused_ref`, command listener, heartbeat watcher |
| 3 | Shared | Pause client library (used by trainer to send pause/resume + heartbeat) |
| 4 | Config | `scarguard.yml` schema: `training` section, credential redaction, config form tab |
| 5 | Trainer | Service scaffolding: Dockerfile, entrypoint, job consumer loop, health check |
| 6 | Trainer | Video processor: frame extraction, two-pass inference, dedupe |
| 7 | Web | Upload endpoints + upload list page |
| 8 | Web | Labeling queue UI + review endpoints |
| 9 | Trainer | Training job runner: pause → prepare_dataset → train → resume |
| 10 | Pipeline | `prepare_dataset.py`: `--local-db`, `--training-uploads-db`, new source function |
| 11 | Web | Training jobs panel + SSE progress + detector state badge |
| 12 | Compose | `docker-compose.yml` + `docker-compose.gpu.yml` trainer service |
| 13 | Tests | Full test suite per section 12 |
| 14 | Docs | README update, CONFIG_REFERENCE update |

---

## Resolved Decisions

1. **Max video duration: 1 minute.**  Upload handler rejects videos over
   60 seconds (configurable via `training.video.max_duration_seconds`).
   At 30 fps that's ≤ 1,800 frames / ~180 MB of JPEGs — bounded.

2. **Background sample volume: auto-sample every Nth frame** (default
   N=10, configurable via `training.video.background_sample_interval`).
   A 1-minute background upload yields ~180 negative samples instead of
   1,800.

3. **Trainer: always-on.**  Starts with `docker compose up`, idles at
   ~50 MB with no CUDA context until a job starts.
