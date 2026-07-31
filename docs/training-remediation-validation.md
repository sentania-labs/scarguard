# Training remediation target-validation runbook

This runbook is intentionally **not executed during implementation**. It drives
the production-class Jetson GPU, detector lifecycle, training jobs, and a
controlled CUDA allocation failure. Run it only inside a new, explicit
captain-approved maintenance window on the L4T 36.4.7 target.

## Acceptance model

Keep the historical mechanisms distinct while validating:

- July 10-13: the kernel sent `SIGKILL` during host-global RAM/swap exhaustion.
  July 10-11 included detector auto-resume; July 13 did not.
- July 30: a CUDA allocation failed and exited 1. PyTorch 2.4.0's unsupported
  Jetson NVML process query then masked the useful allocator diagnostic. It was
  not a kernel OOM.

`SIGKILL` alone must never produce `probable_oom=true`. A CUDA allocation failure
must remain separate from any `diagnostic_masking` field.

## Before the window

1. Obtain the approved start/end time and name an operator responsible for
   restoring detection.
2. Back up `/data/scarguard.db`, `/data/training_workspace`, and the current
   production model using the normal backup procedure.
3. Prepare a tiny disposable YOLO dataset and a distinct output model name.
   Do not reuse or overwrite the production merged dataset or model.
4. Record the deployed image digests and current detector running state.
5. Confirm no training job or Orin CI GPU lease is active.
6. Confirm `TRAINING_CONTROLLER_TOKEN` is a dedicated value of at least 32
   characters and is present only in trainer and training-controller.
7. Apply the `orin-maintenance-approved` PR label only after the window opens.
   It enables the otherwise-skipped trainer image and detector target jobs;
   remove it when the window closes.

## Static deployment checks

From the checked-out release on the target:

```bash
docker compose --profile training config --quiet
docker image inspect ghcr.io/sentania-labs/scarguard-trainer:${IMAGE_TAG} \
  --format '{{json .Config.Labels}}'
docker compose --profile training run --rm --no-deps trainer \
  python3 -c 'import importlib.metadata as m,torch; print(torch.__version__, torch.version.cuda, m.version("ultralytics"))'
```

Require the digest-pinned L4T base identity, Torch 2.4.0/CUDA 12.6, and
Ultralytics 8.4.56. Compare `/app/training-stack.freeze` and
`/app/os-packages.manifest` to the build logs/SBOM before accepting a target
image. Do not use `pip install --upgrade torch` as a remedy.

## Controller ownership matrix

Use disposable 32-character lowercase hexadecimal job IDs. For each case,
inspect `docker compose ps detector` and the controller state volume before and
afterward.

1. Detector initially running: acquire a lease through the trainer's
   `DetectorControllerClient`; require the detector container to stop. Release;
   require the same container ID to restart.
2. Detector intentionally stopped by the operator: acquire and release a lease;
   require it to remain stopped. Start it manually only after recording that the
   controller correctly declined to do so.
3. Detector initially running: acquire, then terminate the disposable trainer
   client without release. After the 120-second stale interval, require the
   controller to restore the same detector container.
4. Acquire under owner A and attempt release under owner B; require HTTP 409 and
   no detector state change. Then release as A.

If any case fails, restore the detector with the normal Compose command, save the
controller state/logs, and stop the window. Do not bypass the controller for a
real training run.

## One-epoch compatibility smoke

Submit a **Train Only** job through Admin → Training Jobs using the disposable
dataset/model, epochs 1, batch 2, image size 480, and workers 4. Require:

- preflight evidence persisted before the child starts: `MemAvailable`, swap
  free, cgroup current/peak/events, and the job ID as GPU lease holder;
- admission only after the detector container is stopped;
- completion with detector restoration;
- a durable `/data/training_workspace/logs/<job_id>.log`, a capped Redis tail,
  exact command/cwd/version metadata, return code 0, and readable UI evidence.

## Resume in the original run directory

1. Start a disposable multi-epoch run and terminate it after `last.pt` exists.
2. Confirm the failed job offers one explicit **Resume checkpoint** action; do
   not submit any automatic retry.
3. Select Resume. Require `workers=4`, the checkpoint below
   `/data/training_workspace/runs`, and Ultralytics's `save_dir` to remain the
   original run directory (no new `train-N` directory).
4. Attempt an out-of-tree file and both a checkpoint symlink and a symlinked run
   directory; require rejection before detector teardown.

## Unified-memory soak and classification

Run a representative disposable dataset long enough to cross the historical
epoch 40/50 region. Sample the host and trainer cgroup independently and compare
them with the job metadata. Require at least the configured 1.5 GiB admission
headroom after detector teardown, no swap exhaustion, accurate cgroup peak/event
deltas, and bounded trainer-parent memory.

In a disposable small cgroup, run the fake memory child from the automated test.
Require a surviving controller, `SIGKILL`, positive OOM evidence, and
`probable_oom=true`. Separately kill a child with `SIGKILL` while memory is
healthy; require `probable_oom=false`.

## Jetson NVML masking regression

With the detector isolated and a disposable CUDA limit, induce a small CUDA
allocation failure. Require the final exception to be `torch.OutOfMemoryError`
or equivalent text beginning `CUDA out of memory`, including available memory
figures. The chained traceback may contain `NVML_SUCCESS == r`; structured
metadata must record `cuda_allocation_failure=true` and
`diagnostic_masking=jetson_nvml_process_query`. There must be no kernel OOM
event and no `probable_oom` claim for this exit-1 case.

## Cancellation and closeout

Cancel a disposable multiprocessing child and verify no descendant PIDs remain.
Require detector restoration and a durable final log. Restore the production
model/dataset if they were staged elsewhere, confirm detector health and live
inference, archive the job JSON/controller logs/resource samples, and end the
maintenance window. Calibrate admission thresholds only from this evidence and
commit any configuration change through the normal review path.
