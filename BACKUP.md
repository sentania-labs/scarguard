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
  retention_daily: 14       # keep the last 14 cycle outputs
  retention_weekly: 8       # plus 8 weekly samples beyond that
  compress: true            # gzip the output
```

Files land at `/data/backups/{db_name}/{YYYY-MM-DDTHH-MM-SS}.db.gz`
inside the Docker volume. Rough disk sizing: a year-old production
deployment tends to produce ~200 KB per database per cycle (compressed),
so 14 daily + 8 weekly ≈ 13 MB total for all three DBs at steady state.

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

Use `scripts/restore-from-backup.sh` from the host. The script stops
the services that hold the target DB open, preserves the pre-restore
state in a `.pre-restore` sidecar file, unpacks the backup, runs
`PRAGMA integrity_check`, and restarts. If the integrity check fails,
the script rolls back to the `.pre-restore` copy automatically.

```bash
# List available backups
docker compose run --rm --entrypoint sh backup \
  -c 'ls -1 /data/backups/scarguard/'

# Restore
scripts/restore-from-backup.sh scarguard 2026-04-22T08-00-00.db.gz
scripts/restore-from-backup.sh auth      2026-04-22T08-00-00.db.gz
scripts/restore-from-backup.sh deterrent 2026-04-22T08-00-00.db.gz
```

Once you've verified the restored system, remove the
`.pre-restore` sidecar:

```bash
docker compose run --rm --entrypoint sh backup \
  -c 'rm /data/scarguard.db.pre-restore'
```

## Recovery from suspected corruption

Web startup runs `PRAGMA integrity_check` against each database and
logs the result. If you see `INTEGRITY CHECK FAILED` in the logs:

1. Stop the affected services (`docker compose stop detector web
   notifier deterrent`).
2. `docker compose run --rm --entrypoint sh backup -c 'sqlite3
   /data/scarguard.db "PRAGMA integrity_check"'` — confirm the
   failure.
3. Restore the most recent clean backup with the script above.
4. If no backup exists or all are equally corrupted: you can
   sometimes recover partial data with `sqlite3 ... .recover`;
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
all work. It's 44 bytes of base64 — low-friction to stash.

## Jetson-specific guidance

**SD-card installs should not be production setups.** SD cards have a
high rate of silent corruption under constant write load (which is
what `/data/scarguard.db` in WAL mode is). Options:

1. **Boot from USB SSD.** Repoint `/data` at the SSD and migrate the
   Docker volume: `docker run --rm -v scarguard-data:/src -v
   /mnt/ssd/scarguard-data:/dst alpine cp -a /src/. /dst/`.
2. **Keep the SD boot but move `/data` to SSD** via a bind mount — add
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

Either way, keep the backup sidecar enabled — the backup files
themselves live on the same volume, so a volume loss takes them too
unless you're also doing off-device copies.

## Related

* `SECURITY.md` — secrets handling, including `/data/secret_key`
* `docs/EMERGENCY_OFF.md` — what to do when a sprinkler is stuck on
* `services/backup/src/main.py` — sidecar source if you want to
  understand or extend the backup logic
