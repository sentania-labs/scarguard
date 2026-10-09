# Backup & Restore

As of v1.14, ScarGuard runs a dedicated backup sidecar that performs
SQLite online-backup of every database on a configurable schedule.
Pre-v1.14 there was no documented backup story; a volume-level `rm`
or an SD-card failure on a Jetson lost months of detection history,
training feedback, and (post v0.13) actuation audit trails.

## What's backed up

Three SQLite databases live on the shared `scarguard-data` Docker volume:

| DB | Purpose | Backed up? |
|---|---|---|
| `/data/scarguard.db` | Detection events, training feedback, performance metrics, visit tracking | yes |
| `/data/auth.db` | Users, sessions, API tokens, audit log | yes |
| `/data/deterrent.db` | Actuation event history (each firing, per-device OFF retries, stuck flags) | yes |

**Not backed up:**

| What | Why |
|---|---|
| `/data/snapshots/*.jpg` | Bulky and short-lived. Re-snapshot on next detection. |
| `/models/*.pt` / `.engine` | Rebuildable from the training script or the upstream YOLO repo. |
| `scarguard.yml` | Separate config-snapshot system at `/admin/backups` (v0.9 feature). Secrets encryption (v1.14) means file-copy backups are also safe to move between hosts as long as `/data/secret_key` comes with them. |
| `/data/secret_key` | **Your responsibility** to back up out-of-band. See below. |

## Schedule and retention

Configured under `backup:` in `scarguard.yml`. Defaults:

```yaml
backup:
  enabled: true
  interval_hours: 24        # run once a day
  retention_daily: 14       # keep one recovery point for each of the last 14 days
  retention_weekly: 8       # plus one per ISO week for 8 further weeks
  compress: true            # gzip the output
```

Scheduled snapshots land at
`/data/backups/{db_name}/{YYYY-MM-DDTHH-MM-SS}.db.gz`; manual ones
(admin UI / Redis trigger) carry a `-manual` tag:
`{YYYY-MM-DDTHH-MM-SS}-manual.db.gz`. Retention treats the two kinds
separately:

* **Scheduled:** files are grouped by their `YYYY-MM-DD` prefix and
  the `retention_daily` most recent days are kept. Every snapshot of
  the newest day is kept (with `interval_hours` below 24 you get
  intra-day points for the current day); each older day collapses to
  its newest snapshot. The sidecar runs a cycle every time it starts,
  so several same-day snapshots are normal; they never crowd out
  earlier days. Beyond those days one snapshot per ISO week is kept
  for `retention_weekly` weeks.
* **Manual:** the newest 10 are kept (`MANUAL_RETENTION` in
  `services/backup/src/main.py`), regardless of how many daily points
  exist. Manual backups therefore neither consume nor evict daily
  recovery points.
* Files the sidecar did not name (`YYYY-MM-DDTHH-MM-SS...`) are left
  alone. In-progress `*.tmp` / `*.partial` staging files are left alone
  while they are fresh; one older than an hour can only be the orphan
  of a cycle that died mid-write and is removed.

Every snapshot is `PRAGMA quick_check`ed before it is published under
its final name; a failed check leaves no file behind. Staging files use
unique names, two snapshots in the same second get distinct names, and
the admin page never lists `*.tmp` / `*.partial` files.

Cycles are serialized. A manual trigger that arrives while the
scheduled cycle is running is not dropped: the sidecar publishes a
`queued` status, waits for the running cycle, and then runs. Further
manual triggers while one is already waiting are coalesced into it
(`skipped` status): the waiting cycle will produce an equally fresh
snapshot.

Rough disk sizing: a year-old production deployment tends to produce
~200 KB per database per cycle (compressed), so 14 daily + 8 weekly +
up to 10 manual ≈ 18 MB total for all three DBs at steady state.

## Seeing what's backed up

Admin UI: **Admin → Database Backups** (`/admin/db-backups`) lists
every file with size and timestamp, and exposes download + manual-run
buttons.

Command line:

```bash
docker compose run --rm --entrypoint sh backup \
  -c 'ls -lh /data/backups/*/'
```

## Triggering a backup manually

From the admin UI: **Run Backup Now** on the Database Backups page.
The button publishes a trigger message on Redis; the sidecar picks it
up and runs one cycle outside the normal schedule. Page auto-reloads
after 5 seconds to show the new file.

From the host:

```bash
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" \
  publish scarguard:backup:trigger '{"request_id":"manual-cli"}'
```

## Restoring

Use `scripts/restore-from-backup.sh` from the host (docker compose v2).
It runs `services/backup/src/restore.py` inside the backup image; the
image has no `sqlite3` CLI, so validation uses Python's `sqlite3`
module.

```bash
# List available backups
docker compose run --rm --no-deps --entrypoint sh backup \
  -c 'ls -1 /data/backups/scarguard/'

# Restore
scripts/restore-from-backup.sh scarguard 2026-04-22T08-00-00.db.gz
scripts/restore-from-backup.sh auth      2026-04-22T08-00-00.db.gz
scripts/restore-from-backup.sh deterrent 2026-04-22T08-00-00.db.gz
```

What the script does:

1. Finds which of the services that open the database are running
   (`scarguard.db`: detector, web, notifier, trainer, backup;
   `auth.db`: web, backup; `deterrent.db`: deterrent, web, backup) and
   stops exactly those. The opt-in `trainer` is included only when it
   is running.
2. Runs `restore.py`, which
   * refuses in-progress (`*.tmp`, `*.partial`) or unknown files, an
     existing `.pre-restore` copy from an earlier restore (it is never
     overwritten), and a database some process still holds open
     (checked again right before the swap);
   * unpacks the snapshot into a uniquely named staging file next to
     the target and runs `PRAGMA quick_check` on it **before** touching
     the live files - a corrupt or truncated snapshot is rejected with
     the live database untouched;
   * moves `<db>.db`, `<db>.db-wal` and `<db>.db-shm` aside together as
     `*.pre-restore`. The old WAL is never replayed into the restored
     file, and the three pre-restore files are a coherent rollback
     copy;
   * renames the validated copy into place atomically and fsyncs;
   * on any failure after validation, puts the previous files back
     byte-for-byte (an absent database stays absent).
3. Restarts the services it stopped. This happens from an exit trap,
   so it also happens when the restore is refused or fails: a failed
   restore is a rolled-back restore, not an outage.

`quick_check` is the validation that runs; it is faster than
`integrity_check` and catches a non-database, truncated or
structurally broken file, but not every index inconsistency. Web
startup still runs the full `PRAGMA integrity_check` on every database
(see below), so a deeper problem in a restored file is reported on the
next start.

Once you've verified the restored system, remove the rollback copy:

```bash
docker compose run --rm --no-deps --entrypoint sh backup \
  -c 'rm -f /data/scarguard.db.pre-restore /data/scarguard.db-wal.pre-restore /data/scarguard.db-shm.pre-restore'
```

A second restore of the same database refuses to run while these
files exist.

### Rolling back a restore

To go back to the pre-restore state, stop the same services the
script stopped, put the trio back, and start them again:

```bash
# scarguard.db; add `trainer` when the training profile is in use
docker compose --profile training stop detector web notifier trainer backup
docker compose run --rm --no-deps --entrypoint sh backup -c '
  cd /data &&
  rm -f scarguard.db scarguard.db-wal scarguard.db-shm &&
  for f in scarguard.db scarguard.db-wal scarguard.db-shm; do
    [ -f "$f.pre-restore" ] && mv "$f.pre-restore" "$f"
  done; true'
docker compose --profile training start detector web notifier trainer backup
```

Moving the `-wal` file back with the database is what makes the
rollback complete: the rows that were only in the WAL at restore time
are replayed on the next open, exactly as they would have been.

### Restore limitations

* The restore path is exercised end to end by
  `services/backup/tests/test_restore_regression.py` on a disposable
  dataset (stale WAL, absent target, corrupt and truncated snapshots,
  rename/fsync faults, a database held open by another process, and
  the host script's restart-on-failure path against a stand-in
  `docker`). It has not been run against a production volume.
* The "database still open" guard relies on SQLite's WAL shared-memory
  lock, which every ScarGuard service uses; a connection in rollback
  journal mode is not detected. Stopping the services remains the
  precondition; the guard is a safety net. It queries the lock through
  a read-only descriptor, so a `-shm` created by the root-run trainer
  is covered too. If the query itself cannot run (unreadable `-shm`,
  unexpected platform), the restore refuses; after stopping every
  service you can bypass the check with `RESTORE_SKIP_OPEN_CHECK=1`
  in the restore command's environment.
* If a failed restore cannot put a `.pre-restore` file back (disk
  fault), the error names exactly which file still holds the previous
  state; follow "Rolling back a restore" for it.
* If the restore container is killed between moving the live files
  aside and renaming the staged copy in, the database is absent and
  the `.pre-restore` trio holds the previous state: follow "Rolling
  back a restore".

## Recovery from suspected corruption

Web startup runs `PRAGMA integrity_check` against each database and
logs the result. If you see `INTEGRITY CHECK FAILED` in the logs:

1. Stop the affected services (`docker compose stop detector web
   notifier deterrent`).
2. Confirm the failure. The backup image has no `sqlite3` CLI; use
   Python:
   ```bash
   docker compose run --rm --no-deps --entrypoint python backup -c '
   import sqlite3
   c = sqlite3.connect("file:/data/scarguard.db?mode=ro", uri=True)
   print(c.execute("PRAGMA integrity_check").fetchall())'
   ```
3. Restore the most recent clean backup with the script above.
4. If no backup exists or all are equally corrupted: you can
   sometimes recover partial data with the `sqlite3` CLI's `.recover`
   command on a copy of the file (on the host, not in the image);
   otherwise start fresh and accept the data loss.

## Off-device backups

The sidecar writes to the same volume that contains the source DBs.
For real disaster resilience (host destroyed, volume lost), replicate
the backup directory off-host. A few approaches:

**rsync to a NAS (simplest):**

```bash
# On the host, as a systemd timer or cron job
docker run --rm -v scarguard-data:/src:ro -v /mnt/nas/scarguard:/dst \
  alpine sh -c 'cp -a /src/backups/. /dst/'
```

**rclone to object storage:**

```bash
# AWS S3, Backblaze B2, or any rclone-supported target
docker run --rm \
  -v scarguard-data:/src:ro \
  -v ~/.config/rclone:/config/rclone \
  rclone/rclone copy /src/backups/ b2:my-bucket/scarguard-backups/
```

**Keep `/data/secret_key` somewhere independent.** If you lose it,
every encrypted secret in `scarguard.yml` becomes unreadable. A
password manager entry, a printed QR code, or an offline USB drive
all work. It's 44 bytes of base64, low-friction to stash.

## Jetson-specific guidance

**SD-card installs should not be production setups.** SD cards have a
high rate of silent corruption under constant write load (which is
what `/data/scarguard.db` in WAL mode is). Options:

1. **Boot from USB SSD.** Repoint `/data` at the SSD and migrate the
   Docker volume: `docker run --rm -v scarguard-data:/src -v
   /mnt/ssd/scarguard-data:/dst alpine cp -a /src/. /dst/`.
2. **Keep the SD boot but move `/data` to SSD** via a bind mount: add
   to `docker-compose.yml`:
   ```yaml
   volumes:
     scarguard-data:
       driver: local
       driver_opts:
         type: none
         o: bind
         device: /mnt/ssd/scarguard-data
   ```

Either way, keep the backup sidecar enabled, the backup files
themselves live on the same volume, so a volume loss takes them too
unless you're also doing off-device copies.

## Related

* `SECURITY.md`: secrets handling, including `/data/secret_key`
* `docs/EMERGENCY_OFF.md`: what to do when a sprinkler is stuck on
* `services/backup/src/main.py`: sidecar source if you want to
  understand or extend the backup logic
