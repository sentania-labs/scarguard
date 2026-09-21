# Emergency OFF: Runbook

**If water is actively hitting fish or property right now, skip to the
fastest stop (step 1), then read the rest.**

## Fastest stop (≤ 30 seconds)

**Option A, web UI (the usual one):**

1. Browse to **Deterrent** admin page (`/admin/deterrent`).
2. Click **⚠ Emergency Off**. Confirm.
3. Observe the status line: "OFF sent to N device(s)."

The endpoint (`/admin/deterrent/force-off`) sends an OFF command to
**every configured device**, regardless of armed/disarmed state or
enabled flag. Retried up to 4× per device via the cloud API (1s, 2s,
4s backoff).

**As of v1.17 it also stops the sequence that was firing.** Before
v1.17 it sent OFF and nothing else: a group working a position read
no change and switched the devices it had just been told to stop
straight back on, so the button appeared to work for a moment and
then undid itself. It now also:

- stops a running group sequence from picking up its next device,
- cancels a test-fire that is still queued behind a detection.

**v1.17.1 closes the last gap between the abort check and ON.** Cancellation
is checked while holding the same cloud-command lock used for OFF. If the
emergency request wins, the old activation sends no ON. If ON was already
admitted, its cloud call finishes before emergency OFF is sent. There is no
stale ON after that emergency OFF completes.

Emergency OFF does send OFF to an active device; the worker may still finish
its local duration wait and issue its normal redundant OFF. The lock is not
held during that wait or retry backoff. Cloud acknowledgement is not proof
that a sleeping battery device has physically shut off, so observe the valve.
A new detection after emergency-off starts under the new generation and can
fire normally; disarm separately to prevent future detections from firing.

If you are on v1.16.12 or earlier and water is hitting fish, **go
straight to Option B**. On those versions the web button will not
stop a group sequence.

**Option B, host shell (if web UI is unreachable):**

```bash
docker compose stop deterrent
```

This kills the deterrent process outright. Any in-flight
`activate_device` call that's currently in the HOLD phase won't run
its OFF command, but **the per-activation watchdog timer fires
unconditional OFF at 60 seconds from ON send**, so the device will
be switched off within a minute of the ON command being issued, even
with the deterrent container gone.

**Option C, Tuya app (total failure):**

If nothing in the ScarGuard stack responds, open the Tuya Smart Life
app on your phone. Each configured device appears; tap it and hit the
power toggle. Or just yank the sprinkler's power plug from the wall.

## Why all three exist

The web-UI emergency-off uses the same Tuya Cloud pipeline as the
normal OFF path. If Redis, the deterrent container, or the Tuya Cloud
API is degraded, the endpoint will fail. Knowing `docker compose stop`
works even without Redis (because of the activation watchdog) means
you always have a second-level stop.

The Tuya app is the third level, it bypasses ScarGuard entirely and
talks to Tuya Cloud directly. If the app can't reach the device
either, the device or its upstream is offline; at that point your only
option is physical (unplug, close a valve, etc.).

## After the emergency

Once the device is confirmed OFF:

1. **Check the actuation log** (`/deterrent-log`). Entries with the
   `stuck` flag set are the ones where the normal OFF failed and the
   watchdog or emergency-off took over. They're marked in the
   device-actions sub-table.
2. **Check `docker compose logs deterrent | grep -i 'WATCHDOG\|stuck'`**.
   Watchdog firings log at CRITICAL; stuck-event publishes log at
   WARNING. You should see the matching entry for whichever layer
   rescued the situation.
3. **Check device status** (**Deterrent → Check Status**). If a device
   still reports `switch_state: true` in the cloud, the physical
   device is still energised. Power-cycle it.
4. **If this keeps happening:** the Tuya Cloud might be flaky or your
   WiFi/router might be dropping commands. Turn on the firmware-side
   auto-off timer in the Tuya app as a belt-and-suspenders, the
   device will switch off on its own after a bounded time even if
   no ScarGuard layer ever sends OFF.

## Other scenarios

### Notifier is spamming

**Symptom:** hundreds of emails/Discord messages in a short span;
multiple channel dispatches for the same event.

1. **Disable the noisy channel** from the web UI: **Config →
   Notifications**, uncheck `enabled` for the channel, save.
2. **Or kill the notifier container:**
   `docker compose stop notifier`.
3. Investigate: was the channel URL pointed at a relay? Did the
   detector start hallucinating detections (camera changed, IR mode,
   etc.)? See `STATUS.md` for common causes.

### Suspicious admin login / suspect session theft

1. **Disable the user immediately** in the web UI: **Admin → Users**,
   set `disabled: true`. This kills all their active sessions.
2. **Rotate infrastructure secrets** in case the attacker scraped them:
   - Regenerate `REDIS_PASSWORD` (setup.sh: re-run with the backfill
     paths, or manually edit `.env`).
   - Regenerate `DETECTION_HMAC_KEY` (same).
   - Optionally rotate `/data/secret_key`: any already-encrypted
     secrets in `scarguard.yml` become unreadable and will need to
     be re-entered (Tuya, SMTP, webhook).
   - Delete `/data/csrf_secret` and restart web.
   - Restart all services: `docker compose down && docker compose up -d`.
3. **Review the audit log** (`/admin/audit-log`): look at
   `login.success` / `login.failure`, `config.save`, `user.*`,
   `api_token.create` since the suspicious window.

### Detector keeps crashing

**Symptom:** healthcheck fails repeatedly, container restarts.

1. `docker compose logs detector --tail 200`: look for the last
   stack trace before the restart.
2. Common causes:
   - **GPU driver state**: reboot the host.
   - **OOM**: v1.14 adds `mem_limit: 3g`; if the detector is being
     killed by the OOM killer the log will show it. Raise the limit
     via a compose override if you have headroom on the host.
   - **Malformed RTSP URL**: v1.14 adds scheme validation, but
     legacy configs may still pass validation and hang FFmpeg. Check
     `scarguard.yml` cameras vs your actual camera endpoints.

### Nothing in the stack responds

Last-resort host reboot:

```bash
docker compose down
# Verify no containers running
docker ps
# Bring it back
docker compose up -d
```

If that doesn't help, `sudo reboot`. After the reboot, verify the
backup sidecar is running (`docker compose ps backup`) and run a
manual backup as a snapshot in case something's about to fall over.

## See also

* `SECURITY.md`: the trust model these scenarios defend against
* `BACKUP.md`: how to roll back a compromised or corrupted DB
* `INFRASTRUCTURE.md`: resource limits, trusted-proxies, host-setup
