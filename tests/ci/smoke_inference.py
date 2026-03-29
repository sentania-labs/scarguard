#!/usr/bin/env python3
"""CI smoke test: load yolov8n, run inference on synthetic frames, print timing.

Outputs a BENCHMARK_JSON= line that the release workflow parses to append
a row to BENCHMARKS.md.

Usage (inside a detector container):
    python3 tests/ci/smoke_inference.py
"""

import json
import platform
import time

import numpy as np
import torch
from ultralytics import YOLO

NUM_FRAMES = 10
FRAME_HEIGHT = 480
FRAME_WIDTH = 640
CONFIDENCE = 0.25

arch = platform.machine()
cpu_name = platform.processor() or "unknown"
cuda = torch.cuda.is_available()
gpu_name = torch.cuda.get_device_name(0) if cuda else None

print(f"Arch:     {arch}")
print(f"CPU:      {cpu_name}")
print(f"PyTorch:  {torch.__version__}")
print(f"CUDA:     {cuda}")
if gpu_name:
    print(f"GPU:      {gpu_name}")

# Download yolov8n (~6 MB) — same starter model used by setup.sh
model = YOLO("yolov8n.pt")

# Synthetic frames — deterministic shape, no network download needed
frames = [
    np.random.randint(0, 255, (FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
    for _ in range(NUM_FRAMES)
]

# Warm-up (first inference triggers model compilation / TensorRT export)
model.predict(frames[0], conf=CONFIDENCE, verbose=False)

# Benchmark
start = time.perf_counter()
for frame in frames:
    model.predict(frame, conf=CONFIDENCE, verbose=False)
elapsed = time.perf_counter() - start

fps = NUM_FRAMES / elapsed
print(f"Frames:   {NUM_FRAMES}")
print(f"Time:     {elapsed:.2f}s")
print(f"FPS:      {fps:.1f}")

# Machine-parseable line for CI to capture
result = {
    "arch": arch,
    "cpu": cpu_name,
    "gpu": gpu_name,
    "device": "cuda" if cuda else "cpu",
    "pytorch": torch.__version__,
    "fps": round(fps, 1),
    "frames": NUM_FRAMES,
    "elapsed_s": round(elapsed, 2),
}
print(f"BENCHMARK_JSON={json.dumps(result)}")
print("SMOKE TEST PASSED")
