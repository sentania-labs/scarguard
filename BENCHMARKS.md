# ScarGuard — Inference Benchmarks

Performance results from CI release builds using `yolov8n.pt` on 10 synthetic 640x480 frames.
Results are appended automatically by the release workflow.

| Release | Arch | Device | CPU / GPU | FPS | Notes |
|---------|------|--------|-----------|-----|-------|
| v0.10.0 | aarch64 | cuda | Orin | 30.9 | GPU inference |
| v0.10.0 | x86_64 | cpu | x86_64 | 1.9 | CPU fallback |
