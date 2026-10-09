#!/usr/bin/env bash
# scripts/restore-from-backup.sh - restore a SQLite DB from a v1.14 backup file.
#
# Stops the services that hold the target DB open, restores from the
# named backup file (gunzipping if needed), and restarts. Run from the
# host with docker compose available.
#
# Usage:
#   scripts/restore-from-backup.sh scarguard 2026-04-22T08-00-00.db.gz
#   scripts/restore-from-backup.sh auth      2026-04-22T08-00-00.db.gz
#   scripts/restore-from-backup.sh deterrent 2026-04-22T08-00-00.db.gz

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
    scarguard) TARGET=/data/scarguard.db; SERVICES=(detector web notifier) ;;
    auth)      TARGET=/data/auth.db;      SERVICES=(web) ;;
    deterrent) TARGET=/data/deterrent.db; SERVICES=(deterrent web) ;;
    *)
        echo "Error: db-name must be scarguard, auth, or deterrent."
        exit 2
        ;;
esac

echo "→ Stopping services that hold ${DB_NAME}.db: ${SERVICES[*]}"
docker compose stop "${SERVICES[@]}"

echo "→ Restoring ${DB_NAME}.db"
# If the python restore script fails, it prints an error and exits with non-zero
docker compose run --rm --entrypoint python backup src/restore.py "${DB_NAME}" "${BACKUP_FILE}"

echo "→ Restarting services: ${SERVICES[*]}"
docker compose start "${SERVICES[@]}"

echo "✓ Restore complete. Pre-restore copy preserved at ${TARGET}.pre-restore"
echo "  Once you've verified the system, remove it with:"
echo "  docker compose run --rm --entrypoint sh backup -c 'rm ${TARGET}.pre-restore'"
