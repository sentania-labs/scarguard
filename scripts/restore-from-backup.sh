#!/usr/bin/env bash
# scripts/restore-from-backup.sh — restore a SQLite DB from a v1.14 backup file.
#
# Stops the services that hold the target DB open, restores from the
# named backup file (gunzipping if needed), and restarts. Run from the
# host with docker compose available.
#
# Usage:
#   scripts/restore-from-backup.sh scarguard 2026-04-22T08-00-00.db.gz
#   scripts/restore-from-backup.sh auth      2026-04-22T08-00-00.db.gz
#   scripts/restore-from-backup.sh deterrent 2026-04-22T08-00-00.db.gz
#
# Backups live inside the scarguard-data named volume at
# /data/backups/{db}/{filename} — list them with:
#   docker compose run --rm --entrypoint sh backup -c 'ls -1 /data/backups/scarguard'

set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <db-name> <backup-filename>"
    echo "  db-name: scarguard | auth | deterrent"
    echo "  backup-filename: e.g. 2026-04-22T08-00-00.db.gz"
    exit 1
fi

DB_NAME=$1
BACKUP_FILE=$2

case "$DB_NAME" in
    scarguard|auth|deterrent) ;;
    *)
        echo "Error: db-name must be scarguard, auth, or deterrent."
        exit 2
        ;;
esac

case "$DB_NAME" in
    scarguard) TARGET=/data/scarguard.db; SERVICES=(detector web notifier) ;;
    auth)      TARGET=/data/auth.db;      SERVICES=(web) ;;
    deterrent) TARGET=/data/deterrent.db; SERVICES=(deterrent web) ;;
esac

SOURCE="/data/backups/${DB_NAME}/${BACKUP_FILE}"

echo "→ Stopping services that hold ${DB_NAME}.db: ${SERVICES[*]}"
docker compose stop "${SERVICES[@]}"

echo "→ Backing up the current ${DB_NAME}.db to ${TARGET}.pre-restore"
docker compose run --rm --entrypoint sh backup -c "
    set -e
    if [ -f '${TARGET}' ]; then
        cp '${TARGET}' '${TARGET}.pre-restore'
    fi
    if [ ! -f '${SOURCE}' ]; then
        echo 'Backup file ${SOURCE} not found — listing available:'
        ls -1 /data/backups/${DB_NAME}/ 2>/dev/null || echo '(no backups for ${DB_NAME})'
        exit 3
    fi
    if echo '${BACKUP_FILE}' | grep -q '\.gz\$'; then
        gunzip -c '${SOURCE}' > '${TARGET}'
    else
        cp '${SOURCE}' '${TARGET}'
    fi
    echo 'Restored ${TARGET} from ${SOURCE}'
"

echo "→ Verifying restored DB integrity"
# SQLite's integrity_check can succeed at the process level (exit 0) while
# still reporting corruption rows in stdout.  We must inspect the actual
# output and require a single "ok" line.
INTEGRITY_OUT=$(docker compose run --rm --entrypoint sh backup -c "
    sqlite3 '${TARGET}' 'PRAGMA integrity_check'
" 2>&1) || true
INTEGRITY_FIRST=$(echo "${INTEGRITY_OUT}" | head -1 | tr -d '\r')
if [[ "${INTEGRITY_FIRST}" != "ok" ]]; then
    echo "WARNING: integrity_check failed — restoring pre-restore backup"
    echo "    sqlite3 output: ${INTEGRITY_OUT}"
    docker compose run --rm --entrypoint sh backup -c "
        cp '${TARGET}.pre-restore' '${TARGET}'
    "
    docker compose start "${SERVICES[@]}"
    exit 4
fi

echo "→ Restarting services: ${SERVICES[*]}"
docker compose start "${SERVICES[@]}"

echo "✓ Restore complete. Pre-restore copy preserved at ${TARGET}.pre-restore"
echo "  Once you've verified the system, remove it with:"
echo "  docker compose run --rm --entrypoint sh backup -c 'rm ${TARGET}.pre-restore'"
