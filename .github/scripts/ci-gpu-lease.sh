#!/usr/bin/env bash
# GPU lease for CI jobs on the Orin runner.
#
# The Orin's 8GB unified memory holds exactly one GPU workload: the live
# detector, a training run, or a CI inference benchmark.  This script makes
# CI a proper tenant of the detector pause protocol (shared/pause_protocol.py)
# instead of barging onto a busy GPU:
#
#   acquire — atomically claim the heartbeat key (waits out an active
#             training run), pause the detector, wait for its ack.
#   refresh — re-arm the heartbeat TTL mid-lease (long retry loops).
#   release — resume the detector only if THIS run paused it, drop the
#             claim.  Never fails the job.
#
# Crash safety: the heartbeat key carries a TTL and the detector auto-resumes
# when it expires, so a killed CI job cannot leave production paused.
#
# Redis access goes through `docker exec` into the production redis container
# (the runner talks to the host Docker daemon); that container's own
# REDIS_PASSWORD env is used, so no secret is needed on the CI side.

set -euo pipefail

ACTION="${1:?usage: ci-gpu-lease.sh acquire|refresh|release}"
REQUEST_ID="ci-${GITHUB_RUN_ID:-local}"

COMMAND_CHANNEL="scarguard:detector:command"
STATE_KEY="scarguard:detector:state"
HEARTBEAT_KEY="scarguard:trainer:heartbeat"

TRAINER_WAIT_SECS=600  # give up if another tenant holds the GPU this long
PAUSE_ACK_SECS=60      # detector normally acks a pause within seconds
LEASE_TTL=600          # heartbeat TTL — detector auto-resumes if CI dies
PAUSE_TIMEOUT=1800     # detector-side ceiling on the pause itself

# The Orin hosts exactly one compose stack; if that ever changes, add a
# com.docker.compose.project filter here.
RID="$(docker ps -q --filter label=com.docker.compose.service=redis | head -n1)"

if [ -z "$RID" ]; then
  echo "No scarguard redis container running — production stack is down, GPU is free."
  exit 0
fi

rcli() {
  docker exec "$RID" sh -c 'exec redis-cli ${REDIS_PASSWORD:+-a "$REDIS_PASSWORD"} --no-auth-warning "$@"' sh "$@"
}

# True when the state key shows a pause acknowledged for OUR request id.
we_paused() {
  local state
  state="$(rcli GET "$STATE_KEY" || true)"
  echo "$state" | grep -q '"state": "paused"' \
    && echo "$state" | grep -q "\"request_id\": \"$REQUEST_ID\""
}

case "$ACTION" in
  acquire)
    # 1. Claim the lease.  SET NX fails while a training run (or another CI
    #    job) holds the heartbeat key, so wait for it to clear.
    deadline=$(( $(date +%s) + TRAINER_WAIT_SECS ))
    while :; do
      if [ "$(rcli SET "$HEARTBEAT_KEY" "$REQUEST_ID" NX EX "$LEASE_TTL")" = "OK" ]; then
        # Claimed — but if the detector is already paused under a foreign
        # request id, either that requester just crashed (heartbeat expired
        # before the detector auto-resumed) or a trainer acked its pause
        # milliseconds before writing its first heartbeat.  Wait one trainer
        # heartbeat interval to tell them apart: a live trainer's plain SET
        # overwrites our claim.
        state="$(rcli GET "$STATE_KEY" || true)"
        if echo "$state" | grep -q '"state": "paused"' && ! we_paused; then
          echo "Detector paused under a foreign request id — checking for a live holder (35s)..."
          sleep 35
          if [ "$(rcli GET "$HEARTBEAT_KEY" || true)" = "$REQUEST_ID" ]; then
            echo "Stale pause (holder crashed) — taking over the lease; GPU is already free."
            exit 0
          fi
          echo "A live trainer reclaimed the lease — waiting."
        else
          break
        fi
      else
        echo "GPU lease busy (holder: $(rcli GET "$HEARTBEAT_KEY" || true)) — retrying in 30s"
        sleep 30
      fi
      if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "::error::GPU lease held by '$(rcli GET "$HEARTBEAT_KEY" || true)' for over ${TRAINER_WAIT_SECS}s (training run?) — re-run this job once it finishes."
        exit 1
      fi
    done
    echo "GPU lease claimed ($REQUEST_ID, TTL ${LEASE_TTL}s)"

    # 2. Pause the detector and wait for its ack (drain + model unload takes
    #    a few seconds).  PUBLISH returns the subscriber count — zero means
    #    no detector is listening and the GPU is already free.  The command
    #    is re-published every poll round: a pause that raced an auto-resume
    #    is silently ignored by the detector, and re-sending converges.
    deadline=$(( $(date +%s) + PAUSE_ACK_SECS ))
    while :; do
      subs="$(rcli PUBLISH "$COMMAND_CHANNEL" "{\"action\": \"pause\", \"request_id\": \"$REQUEST_ID\", \"timeout\": $PAUSE_TIMEOUT}")"
      if [ "$subs" = "0" ]; then
        echo "No detector subscribed on $COMMAND_CHANNEL — nothing to pause."
        exit 0
      fi
      for _ in 1 2 3 4 5; do
        if we_paused; then
          echo "Detector paused — GPU released for CI."
          exit 0
        fi
        sleep 2
      done
      if [ "$(date +%s)" -ge "$deadline" ]; then
        # Drop the claim only if it is still ours — a trainer that paused
        # concurrently owns the key now, and its heartbeat must survive.
        if [ "$(rcli GET "$HEARTBEAT_KEY" || true)" = "$REQUEST_ID" ]; then
          rcli DEL "$HEARTBEAT_KEY" > /dev/null || true
        fi
        echo "::error::Detector did not ack pause within ${PAUSE_ACK_SECS}s (state: $(rcli GET "$STATE_KEY" || true))"
        exit 1
      fi
    done
    ;;

  refresh)
    # Re-arm the TTL so a long benchmark retry loop can't outlive the lease
    # and trigger a mid-benchmark auto-resume.  Only touches our own claim.
    if [ "$(rcli GET "$HEARTBEAT_KEY" || true)" = "$REQUEST_ID" ]; then
      rcli SET "$HEARTBEAT_KEY" "$REQUEST_ID" XX EX "$LEASE_TTL" > /dev/null || true
      echo "GPU lease refreshed (TTL ${LEASE_TTL}s)"
    fi
    exit 0
    ;;

  release)
    # Best-effort: the heartbeat TTL and detector pause-timeout are the
    # backstops, so release never fails the job.
    holder="$(rcli GET "$HEARTBEAT_KEY" || true)"
    state="$(rcli GET "$STATE_KEY" || true)"
    if [ "$holder" = "$REQUEST_ID" ]; then
      rcli DEL "$HEARTBEAT_KEY" > /dev/null || true
    fi
    # Resume ONLY a pause this run is responsible for: one acked under our
    # request id, or a stale pause we took over (we held the heartbeat while
    # the state carried a dead requester's id).  Resuming unconditionally
    # would restart the detector under a live training run whenever acquire
    # timed out waiting for it — detector + training on the GPU together is
    # the OOM this lease exists to prevent.
    if echo "$state" | grep -q '"state": "paused"' \
        && { echo "$state" | grep -q "\"request_id\": \"$REQUEST_ID\"" || [ "$holder" = "$REQUEST_ID" ]; }; then
      rcli PUBLISH "$COMMAND_CHANNEL" "{\"action\": \"resume\", \"request_id\": \"$REQUEST_ID\"}" > /dev/null || true
      for _ in $(seq 1 15); do
        if rcli GET "$STATE_KEY" | grep -q '"state": "running"'; then
          echo "Detector resumed."
          exit 0
        fi
        sleep 2
      done
      echo "::warning::Detector did not confirm resume — the heartbeat TTL will auto-resume it."
    else
      echo "This run does not hold the detector pause — leaving its state alone."
    fi
    exit 0
    ;;

  *)
    echo "usage: ci-gpu-lease.sh acquire|refresh|release" >&2
    exit 1
    ;;
esac
