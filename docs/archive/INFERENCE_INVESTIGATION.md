# Inference Time Regression Investigation

**Status:** ✅ RESOLVED — root cause found 2026-04-07 00:30 UTC, fix landing in v0.12.7
**Started:** 2026-04-06
**Affected:** Detector service, all v0.12.1 – v0.12.6 releases (any camera load, any uptime)
**Introduced by:** commit `149374d` — *fix: redirect ultralytics runs dir to /tmp for non-root containers* (#81)

---

## Problem

Inference time per camera frame jumped from ~50ms (v0.11.0 baseline) to ~150-165ms (v0.12.x) on the Jetson Orin Nano. Model is `yolov8n.pt`, never an `.engine` file. The `avg_inference_ms` metric shown in the stats UI wraps the entire `detector.predict()` call (including any lock wait).

---

## Timeline (UTC)

| When | Event | pond-south inference |
|------|-------|---------------------|
| Mar 31 – Apr 5 | v0.11.0 running, 2 cameras | ~50 ms |
| Apr 5 ~17:50 | Other-service deploy (web/notifier?) | ~50 ms |
| Apr 6 01:45 | User upgrades to v0.12.0; **fails immediately** (issue #77 — non-root permissions) | (outage) |
| Apr 6 ~01:55 | Third camera (`front-door`) added during recovery work | — |
| Apr 6 ~04:10 | Recovery with v0.12.1/v0.12.2 | ~165 ms |
| Apr 6 11:07+ | v0.12.3, v0.12.4 deployed | ~165 ms |
| Apr 6 ~13:00 | apt-get update + upgrade on Orin | ~165 ms |
| Apr 6 (later) | Disable front-door camera (3 → 2 cameras) | ~150 ms (-15) |
| Apr 6 (later) | Disable pond-north (2 → 1 camera) | ~127 ms (-23) |

**Key takeaway:** dropping from 3 → 1 cameras only saves ~38ms. Even with zero contention, single-camera inference is still **2.5x the v0.11.0 baseline**.

---

## Hypotheses Ruled Out

### ❌ Model format (TensorRT vs PyTorch)
Always been `.pt` (`yolov8n.pt`), never an `.engine`. User confirmed config has been unchanged.

### ❌ CUDA / GPU access
```
docker exec scarguard-detector-1 python3 -c "import torch; print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0))"
# CUDA available: True
# Device: Orin
```
Non-root container has full GPU access via NVIDIA runtime.

### ❌ Lock contention
With `frame_skip=15` and the v0.12.3 `grab()` optimization, we hypothesized that fast skip-cycles caused threads to pile up on the model's `FairLock`. But:
- 3 cameras → 168 ms
- 2 cameras → 150 ms
- 1 camera → **127 ms** (zero contention possible)

The contention contribution is ~38ms; the bulk of the regression (77ms) persists with one camera.

### ❌ Ultralytics version
v0.11.0 had unconstrained `ultralytics` (resolved to 8.4.27 / 8.4.31 depending on image build date). v0.12.x pinned `~=8.3.0` (resolved to 8.3.253). We benchmarked both side by side:

**x86 CPU benchmark (60s video, ~1600 frames):**

| Version | ultralytics | Mean | Median | P95 |
|---------|-------------|------|--------|-----|
| v0.11.0 | 8.4.31 | **496.0 ms** | 458.7 | 746.7 |
| v0.12.5 | 8.3.253 | **499.5 ms** | 470.7 | 722.0 |

**Orin GPU benchmark (50 iterations on a real captured snapshot, 576×1024):**

| Version | ultralytics | Mean | Median | P95 |
|---------|-------------|------|--------|-----|
| v0.11.0 | 8.4.27 | **30.3 ms** | 28.1 | 50.5 |
| v0.12.5 | 8.3.253 | **30.9 ms** | 28.8 | 41.6 |

**Identical.** Ultralytics version is not the cause.

### ❌ PyTorch / CUDA / cuDNN versions
```
v0.11.0 image:  torch 2.4.0 / cuda 12.6 / cudnn 90400
v0.12.5 image:  torch 2.4.0 / cuda 12.6 / cudnn 90400
```
Same. (Both built on `dustynv/l4t-pytorch:r36.4.0`. Base image was unpinned in v0.11.0–v0.12.2; digest pinned starting v0.12.3 in commit `b33b6da`. Not relevant since the underlying versions match.)

### ❌ Apt-get upgrade on the Orin
Done at ~13:00 UTC Apr 6. The regression was already present at 04:10 UTC. Not the trigger (though we haven't fully ruled out it making things worse).

---

## ⚠️ Major Finding (Apr 6 evening session)

Same bench script, same RTSP frame (1024×576 — confirmed; **resolution hypothesis is dead**, cameras already run at the size we benchmarked), same model:

| Where bench runs | Mean | Median | P95 |
|------------------|------|--------|-----|
| **Fresh** v0.12.5 container, detector NOT running | **31.5 ms** | 29.0 | 40.1 |
| **Inside the live** scarguard-detector-1 container (separate Python process) | **91.3 ms** | 89.0 | 107.2 |

**Δ = ~60 ms** purely from contention with the running detector process. Live `infer_ms` is ~127 ms with 1 camera, so the remaining ~36 ms is the in-process FairLock + post-filter + intra-process GIL contention.

### What's burning the CPU
`ps -eLf` inside the live 1-camera container shows **21 threads**, with one thread (LWP 41) at a steady **26 % CPU** and 6 more at 2–3 %. The hot thread is the camera worker loop in `services/detector/src/main.py:253-261`:

```python
while not stop_event.is_set():
    frame_count += 1
    if frame_count % frame_skip_ref.get() != 0:
        if not stream.grab():
            ...
        continue   # NO sleep — loops as fast as grab() returns
    ret, frame = stream.read()
```

With `frame_skip=15`, 14 out of every 15 iterations call `stream.grab()` and immediately re-loop. `cv2.VideoCapture.grab()` with the FFmpeg backend does **not** honour `CAP_PROP_BUFFERSIZE=1` (that flag only works for V4L2/DSHOW). FFmpeg buffers RTSP packets internally, so `grab()` drains the buffer as fast as possible — burning a CPU core — then briefly blocks when the buffer empties. The other ~6 hot threads are PyTorch's intra-op pool fighting that core for cycles, which is what stretches each inference call.

In v0.11.0 there was no `grab()` fast-skip path; the camera loop did a full `read()` (decode included) per iteration, which is naturally rate-limited by the stream and didn't burn a core. The `grab()` "optimization" added in v0.12.3 to "save CPU/GPU on skipped frames" actually does the opposite on this RTSP/FFmpeg path.

### Likely fix (not yet implemented)
- Sleep briefly between grabs (`stop_event.wait(1.0 / target_fps)` or similar), OR
- Drop the `grab()` skip path entirely and go back to `read()` + discard, OR
- Set FFmpeg options that actually limit the internal buffer (`OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp|fflags;nobuffer|max_delay;0`) and verify with another `ps -eLf`.

Need to validate the fix by re-running the in-container bench and confirming the 60 ms penalty disappears.

---

## The Gap That Remains

Standalone benchmark on the Orin (real frame, no lock, no other code): **~30 ms**
Live detector with **1 camera** (no lock contention): **~127 ms**

There is a **~95 ms gap** between what the model actually takes and what the running detector measures, even with all contention removed.

Whatever causes this gap is what we're now hunting.

---

## Current Suspects

### 1. RTSP frame resolution / preprocessing
The standalone benchmark used a **576×1024** snapshot file. Real RTSP frames from the UniFi cameras may be much larger (1080p, 2K, or 4K). yolov8n always resizes internally to 640px, but a large input frame requires:
- More cv2 decode work (already excluded from `infer_ms` but worth verifying)
- Larger numpy array passed into ultralytics
- More expensive letterbox/resize inside ultralytics preprocessing

**Action:** check actual frame dimensions coming from RTSP, then re-run the benchmark with a frame at that resolution.

### 2. Per-call overhead in the predict pipeline
Things `detector.predict()` does on top of the raw model call:
- FairLock acquire (cheap when uncontended, but still creates an Event)
- Iterating `result.boxes` and filtering by class/confidence (line ~74-85 of `detector.py`)
- Class name lookup via `result.names[int(box.cls)]`

Cumulatively this should be sub-millisecond, but worth measuring.

### 3. PyTorch CUDA stream / context overhead per call
The standalone benchmark calls `model.predict()` 50 times in a tight loop on the same PyTorch context. The live detector calls it from a long-running thread that may have different GPU context state, especially with the model pool / multiple cameras sharing one model.

### 4. The image isn't blank
We DID test with a real captured snapshot, so this isn't a "blank frame fast path" artifact. But the snapshot may be qualitatively different from a live RTSP frame (different size, different content density → different NMS workload).

---

## Files and Functions Involved

| File | Lines | Role |
|------|-------|------|
| `services/detector/src/main.py` | 282–288 | `infer_ms` measurement (wraps `detector.predict()` including lock wait) |
| `services/detector/src/detector.py` | 42–85 | `YOLODetector.predict()` — lock + model.predict + post-filter |
| `services/detector/src/fair_lock.py` | full | FIFO lock (introduced in v0.12.5) |
| `services/detector/src/stream.py` | 67–115 | RTSP capture; `CAP_PROP_BUFFERSIZE = 1` (line 74) |
| `services/detector/src/stream.py` | 117–136 | `grab()` method (introduced in v0.12.3) |

---

## Reproducible Diagnostic Commands

### Standalone benchmark on Orin (real frame, no contention)
```bash
# Pull both images for comparison
docker pull ghcr.io/sentania-labs/scarguard-detector:v0.11.0
docker pull ghcr.io/sentania-labs/scarguard-detector:v0.12.5

# Extract a real snapshot and the model
docker cp scarguard-detector-1:/data/snapshots/<some-frame>.jpg /tmp/test_frame.jpg
docker cp scarguard-detector-1:/models/yolov8n.pt /tmp/yolov8n.pt

# Save bench script as /tmp/bench_orin.py:
#   import time, sys, cv2
#   from ultralytics import YOLO
#   m = YOLO(sys.argv[1])
#   frame = cv2.imread(sys.argv[2])
#   for _ in range(5): m.predict(frame, verbose=False, save=False, project="/tmp/runs")
#   times = []
#   for _ in range(50):
#       t0 = time.monotonic()
#       m.predict(frame, verbose=False, save=False, project="/tmp/runs")
#       times.append((time.monotonic()-t0)*1000)
#   times.sort()
#   print(f"Mean: {sum(times)/len(times):.1f}ms  Median: {times[len(times)//2]:.1f}ms  P95: {times[int(len(times)*0.95)]:.1f}ms")

docker run --rm --runtime nvidia \
  -v /tmp/bench_orin.py:/bench.py:ro \
  -v /tmp/yolov8n.pt:/models/yolov8n.pt:ro \
  -v /tmp/test_frame.jpg:/frame.jpg:ro \
  ghcr.io/sentania-labs/scarguard-detector:v0.11.0 \
  python3 /bench.py /models/yolov8n.pt /frame.jpg
```

### Compare versions inside the live container
```bash
docker exec scarguard-detector-1 python3 -c "
import torch, ultralytics
print('torch:', torch.__version__)
print('cuda:', torch.version.cuda)
print('cudnn:', torch.backends.cudnn.version())
print('ultralytics:', ultralytics.__version__)
print('cuda_avail:', torch.cuda.is_available())
"
```

---

## Next Steps (obsolete — kept for history)

1. ~~Get the actual RTSP frame resolution~~ — done, both 1024×576, not the cause
2. ~~Re-run the Orin benchmark with a frame matching real RTSP dimensions~~ — done, not the cause
3. ~~Add temporary timing logs to `detector.predict()`~~ — superseded by py-spy (no code change needed)
4. ~~Test bypassing the `cam.resolution` config~~ — not needed

---

## 🎯 RESOLUTION (2026-04-07 00:30 UTC)

**Root cause:** commit `149374d` (*fix: redirect ultralytics runs dir to /tmp for non-root containers*, #81) added `project="/tmp/runs"` to every `model.predict()` call in `detector.py` and `evaluator.py`. With that kwarg present, ultralytics' `Predictor` constructor unconditionally computes `save_dir = increment_path(Path("/tmp/runs") / "predict")` — **even when `save=False`** — and **creates a new `predict{N}` directory on every call**. The `increment_path` helper does:

```python
for n in range(2, 9999):
    p = f"{path}{n}"
    if not os.path.exists(p):
        break
```

So each `predict()` call runs up to **N-1 `os.path.exists()` syscalls** where N is the current count of `predict*` dirs. On the user's Orin we found **9998 directories** in `/tmp/runs`, meaning every predict call was making ~9998 stat() calls just to name the next directory, *then* creating it and continuing.

**Immediate validation on the live v0.12.6 stack** (before any code change):

```bash
find /tmp/runs -mindepth 1 -maxdepth 1 -type d -name 'predict*' -delete
```

| | Before | After |
|---|---|---|
| pond-north `avg_inference_ms` | 149.4 ms | **52.8 ms** |
| pond-south `avg_inference_ms` | 146.1 ms | **50.7 ms** |
| `/tmp/runs/predict*` count | 9998 | 3 |

A simple directory delete recovered **full v0.11.0 performance** (the pre-upgrade baseline was ~50 ms on two cameras). The pond cameras re-accumulated to 168 directories in 45 seconds, so the cleanup alone is not a durable fix — we need the code change as well.

### What the py-spy profile actually showed

30-second sample of the live detector (after ~4 h of uptime, 9998 dirs accumulated), camera-worker threads:

| Stack frame | % wall time |
|---|---|
| `os.path.exists` (genericpath.py:19, called from `increment_path`) | **45-48 %** |
| `increment_path` (ultralytics/utils/files.py:142-143) | 7-8 % |
| `stream.grab` (stream.py:129) | 8-10 % |
| `stream.read` (stream.py:108) | 3-4 % |
| **actual torch work** (`_conv_forward` + `silu` + module `_call_impl`) | **~14-15 %** |
| ultralytics augment/letterbox | 3-4 % |

**The detector was spending 3× more wall-time stat()'ing filesystem paths than running the model.** No amount of FairLock tuning, `grab()`-loop fixing, FFmpeg buffer tweaking, or PyTorch caching-allocator theorizing would have helped — none of those were the problem.

### The code fix (shipped in v0.12.7)

`services/detector/src/detector.py`:

```python
results = self._model.predict(
    frame,
    conf=self.confidence_threshold,
    verbose=False,
    save=False,
    project="/tmp/runs",
    name="predict",
    exist_ok=True,      # ← NEW: reuse existing dir instead of incrementing
)
```

Same change in `services/detector/src/evaluator.py:247`. The `exist_ok=True` keyword tells ultralytics' `increment_path` to skip the increment loop entirely when the target directory already exists. All predict calls now resolve to `/tmp/runs/predict/` (a single fixed directory) and the stat loop runs once to check existence, not 9998 times.

`services/detector/entrypoint.sh` also pre-creates `/tmp/runs/predict/` at startup so the first predict call has a valid directory to find, and defensively removes any leftover `predict[0-9]*` siblings from a previous image if the container is being upgraded in place.

### How we got misled

Documenting the wrong turns so we don't repeat them:

1. **"grab() skip loop burns a core"** — partially true (it does use ~13 % CPU per camera) but the GPU wall-time regression wasn't caused by CPU starvation. The loop was a red herring because it looked like the most obvious code change in v0.12.3.
2. **"Tegra / nvmap / driver state accumulates over Orin uptime"** — wrong. The "climb over time" we watched was really **the `predict*` directory count growing**, and the bench scanning more paths per call. The shape was right; the cause was one layer up from where I was looking.
3. **"Ultralytics 8.3 vs 8.4 differ on libav-backed buffers"** — wrong. The static-JPG benchmark showed both versions identical. The difference I *thought* I saw was because the fresh container had an empty `/tmp/runs` and the `docker exec` bench shared the live container's full `/tmp/runs`.
4. **"`frame.copy()` fixes it"** — wrong. The copy was incidental; what actually made the fast measurement fast was running it in a fresh container with an empty `/tmp/runs`.
5. **"CPU cgroup contention via `docker exec`"** — half right. `docker exec` does share the container's cgroup, and that might contribute a few ms, but the dominant effect was the shared `/tmp/runs` directory. The CPU theory was another pattern-match on a surface symptom.

The common thread: **every theory except the last one was built on inference from single-data-point measurements without ever profiling the running process.** Once we ran `py-spy dump` and `py-spy record` on the live detector, the answer was in the top 10 frames of the output. The investigation took ~4 hours; py-spy would have given us the answer in ~30 seconds. Lesson: **for a "why is this slow" question, profile first, theorize second.**

---

## Upstream ultralytics issue (proposed text)

To be filed at https://github.com/ultralytics/ultralytics/issues once the scarguard fix lands.

> **Title:** `Predictor` runs `increment_path` on every call even when `save=False`, causing O(N) filesystem scans under sustained inference
>
> **Version:** Observed on `ultralytics==8.3.253`. Likely also present on current main — haven't verified.
>
> **Summary**
> When `YOLO.predict(frame, save=False, project=some_dir)` is called in a loop (typical for streaming / live inference), `Predictor.setup_source` / `get_save_dir` calls `increment_path(Path(project) / name)` on **every** call, regardless of the `save=` flag. `increment_path` creates a new `predict{N}` subdirectory under `project` on each call and, on the *next* call, scans `predict2`, `predict3`, ..., `predict{N-1}` via `os.path.exists` until it finds the next unused integer. After `N` calls, each predict is doing roughly `N-1` stat syscalls purely to name a directory that is then never written to.
>
> On a long-running detector process this is unbounded. In our case (three-camera inference, ~6 predicts/sec), the `predict*` directory count reached 9998 after ~24 hours, at which point each `predict()` call was spending ~45-50% of its wall time in `os.path.exists()` (confirmed via py-spy). That translated to a 3× increase in observed inference latency (~50 ms → ~150 ms per call on a Jetson Orin Nano) even though the underlying model cost was unchanged.
>
> **Expected behavior**
> When `save=False`, no save directory is actually used by the inference path, and the predictor should not create or scan for `predict{N}` directories at all. Alternatively, if a save directory must exist for plotting/visualization code paths that are gated on other flags, it should be set up **lazily** on the first call that actually needs it, not eagerly in the predictor constructor on every call.
>
> **Current workaround**
> Users can pass `name="predict", exist_ok=True` as predict kwargs to force `increment_path` to reuse a single directory. This works on 8.3.x and we're shipping it now. But the user has to know the increment exists, and has to know that `exist_ok=True` is the escape hatch — neither of which is discoverable from the API docs.
>
> **Reproducer**
> ```python
> import time
> from ultralytics import YOLO
> import numpy as np
> m = YOLO("yolov8n.pt")
> frame = np.zeros((576, 1024, 3), dtype=np.uint8)
> t = []
> for i in range(5000):
>     t0 = time.perf_counter()
>     m.predict(frame, verbose=False, save=False, project="/tmp/ultralytics_repro")
>     t.append(time.perf_counter() - t0)
>     if i % 500 == 0:
>         print(f"iter {i}: mean last-500 = {sum(t[-500:])/len(t[-500:])*1000:.1f} ms")
> ```
> Expect to see mean time per call grow linearly with `i` on any filesystem, dramatically on overlay2-backed container `/tmp`.
>
> **Proposed fix**
> Gate the `save_dir` setup on `save or save_txt or save_crop or show or ...` — any flag that actually requires an output directory. If none are set, don't call `increment_path` and don't create a directory.

---

## Memory / Cross-Session Tracking

This investigation is also tracked in agent memory at:
`~/.claude/projects/-home-labuser-scarguard/memory/project_inference_regression.md`
