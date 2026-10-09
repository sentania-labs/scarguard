import gzip
import os
import shutil
import sqlite3
import sys
from pathlib import Path


def fsync_file(path: Path):
    if not path.exists():
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def fsync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def do_restore(db_name: str, backup_filename: str, data_dir: Path, backup_root: Path):
    target = data_dir / f"{db_name}.db"
    source = backup_root / db_name / backup_filename

    if not source.exists():
        raise FileNotFoundError(f"Backup file {source} not found")

    target_wal = data_dir / f"{db_name}.db-wal"
    target_shm = data_dir / f"{db_name}.db-shm"

    pre_db = data_dir / f"{db_name}.db.pre-restore"
    pre_wal = data_dir / f"{db_name}.db-wal.pre-restore"
    pre_shm = data_dir / f"{db_name}.db-shm.pre-restore"

    if pre_db.exists():
        pre_db.unlink()
    if pre_wal.exists():
        pre_wal.unlink()
    if pre_shm.exists():
        pre_shm.unlink()

    target_existed = target.exists()

    # Create pre-restore backups
    if target_existed:
        shutil.copy2(target, pre_db)
    if target_wal.exists():
        os.rename(target_wal, pre_wal)
    if target_shm.exists():
        os.rename(target_shm, pre_shm)

    tmp_target = data_dir / f"{db_name}.db.tmp"

    try:
        # Extract or copy
        if backup_filename.endswith(".gz"):
            with gzip.open(source, "rb") as f_in:
                with open(tmp_target, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
        else:
            shutil.copy2(source, tmp_target)

        # Validate
        conn = sqlite3.connect(tmp_target)
        try:
            cursor = conn.execute("PRAGMA quick_check")
            row = cursor.fetchone()
            if not row or row[0] != "ok":
                raise ValueError("integrity check failed")
        finally:
            conn.close()

        # Fsync staged file
        fsync_file(tmp_target)

        # Atomic replacement
        os.replace(tmp_target, target)

        # Fsync dir
        fsync_dir(data_dir)

    except Exception as e:
        if tmp_target.exists():
            tmp_target.unlink()

        # Restore previous state
        if target_existed:
            os.replace(pre_db, target)
        else:
            if target.exists():
                target.unlink()

        if pre_wal.exists():
            os.rename(pre_wal, target_wal)
        if pre_shm.exists():
            os.rename(pre_shm, target_shm)

        raise RuntimeError(f"Restore failed: {e}") from e

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python restore.py <db_name> <backup_filename>")
        sys.exit(1)

    db_name = sys.argv[1]
    backup_filename = sys.argv[2]
    data_dir = Path("/data")
    backup_root = data_dir / "backups"

    do_restore(db_name, backup_filename, data_dir, backup_root)
