#!/usr/bin/env bash
# scripts/restore-from-backup.sh - restore a SQLite DB from a backup snapshot.
#
# Stops every running service that opens the target database, runs the
# Python restore (services/backup/src/restore.py) inside the backup
# image, and restarts exactly the services it stopped. The restart runs
# from an EXIT trap, so a refused or failed restore (restore.py puts the
# previous state back itself) never leaves the stack down.
#
# Run from the host, in the directory you normally run `docker compose`
# from. Requires docker compose v2.
#
# Usage:
#   scripts/restore-from-backup.sh scarguard 2026-04-22T08-00-00.db.gz
#   scripts/restore-from-backup.sh auth      2026-04-22T08-00-00.db.gz
#   scripts/restore-from-backup.sh deterrent 2026-04-22T08-00-00.db.gz
#
# Backups live inside the scarguard-data named volume at
# /data/backups/{db}/{filename} - list them with:
#   docker compose run --rm --no-deps --entrypoint sh backup -c 'ls -1 /data/backups/scarguard'
#
# See BACKUP.md ("Restoring") for what is kept as a rollback copy and
# how to roll back by hand.

set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <db-name> <backup-filename>"
    echo "  db-name: scarguard | auth | deterrent"
    echo "  backup-filename: e.g. 2026-04-22T08-00-00.db.gz"
    exit 1
fi

DB_NAME=$1
BACKUP_FILE=$2

# Every service that opens the database, readers included: a reader keeps
# the WAL/SHM alive and would see the file swap mid-flight. `trainer`
# only exists under the opt-in training profile; enabling the profile on
# every compose call lets the script see and stop it when it is running.
# The backup sidecar is stopped too so no snapshot runs during the swap.
case "$DB_NAME" in
    scarguard) SERVICES=(detector web notifier trainer backup) ;;
    auth)      SERVICES=(web backup) ;;
    deterrent) SERVICES=(deterrent web backup) ;;
    *)
        echo "Error: db-name must be scarguard, auth, or deterrent." >&2
        exit 2
        ;;
esac
TARGET="/data/${DB_NAME}.db"

compose() {
    docker compose --profile training "$@"
}

STOPPED=()

restart_services() {
    if (( ${#STOPPED[@]} == 0 )); then
        return
    fi
    echo "→ Restarting services: ${STOPPED[*]}"
    if ! compose start "${STOPPED[@]}"; then
        echo "WARNING: could not restart ${STOPPED[*]}." >&2
        echo "         Run: docker compose start ${STOPPED[*]}" >&2
    fi
}

on_exit() {
    local rc=$?
    trap - EXIT
    if (( rc != 0 )); then
        echo "✗ Restore did not complete (exit ${rc})." >&2
        echo "  restore.py leaves ${TARGET} as it was, or puts it back, on every failure." >&2
        if (( ${#STOPPED[@]} > 0 )); then
            echo "  The stopped services are restarted now." >&2
        fi
    fi
    restart_services
    exit "${rc}"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "→ Checking which of (${SERVICES[*]}) are running"
RUNNING=$(compose ps --services --status running)
for svc in "${SERVICES[@]}"; do
    if grep -qx "${svc}" <<<"${RUNNING}"; then
        STOPPED+=("${svc}")
    fi
done

if (( ${#STOPPED[@]} > 0 )); then
    echo "→ Stopping services that hold ${DB_NAME}.db: ${STOPPED[*]}"
    compose stop "${STOPPED[@]}"
else
    echo "→ No running service holds ${DB_NAME}.db"
fi

echo "→ Restoring ${TARGET} from /data/backups/${DB_NAME}/${BACKUP_FILE}"
# restore.py validates the snapshot (PRAGMA quick_check) before touching
# the live files, moves db/-wal/-shm aside as *.pre-restore, swaps the
# validated copy in atomically, and rolls back on any failure. A nonzero
# exit lands in the EXIT trap above.
compose run --rm --no-deps --entrypoint python backup src/restore.py "${DB_NAME}" "${BACKUP_FILE}"

echo "✓ Restore complete."
echo "  Rollback copy: ${TARGET}.pre-restore (and ${TARGET}-wal/-shm.pre-restore if they existed)."
echo "  Once you've verified the system, remove it with:"
echo "    docker compose run --rm --no-deps --entrypoint sh backup -c 'rm -f ${TARGET}.pre-restore ${TARGET}-wal.pre-restore ${TARGET}-shm.pre-restore'"
echo "  To undo the restore instead, follow 'Rolling back a restore' in BACKUP.md."
