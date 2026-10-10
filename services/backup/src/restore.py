"""Restore one ScarGuard SQLite database from a backup snapshot.

Invoked inside the backup image by ``scripts/restore-from-backup.sh``
after that script has stopped every service that opens the target
database::

    docker compose run --rm --no-deps --entrypoint python backup \\
        src/restore.py scarguard 2026-04-22T08-00-00.db.gz

Only the Python ``sqlite3`` module is used; the slim image ships no
``sqlite3`` CLI.

Sequence, with every step arranged so that a failure leaves ``/data``
exactly as it was found:

1. Resolve ``/data/backups/<db>/<file>``.  In-progress ``.partial`` and
   ``.tmp`` snapshots and anything containing a path separator are
   refused.
2. Refuse if a ``.pre-restore`` rollback copy from an earlier restore is
   still present (it is never overwritten) or if any process still holds
   the database open (detected through SQLite's shared-memory lock).
3. Unpack the snapshot into a uniquely named staging file next to the
   target, ``PRAGMA quick_check`` it read-only, and fsync it.  A bad
   snapshot is rejected here, before the live files are touched.
4. Move the current ``<db>.db``, ``<db>.db-wal`` and ``<db>.db-shm``
   aside together as ``*.pre-restore``, so the rollback copy is coherent
   and the stale WAL is never replayed into the restored file.
5. ``os.replace`` the staging file over the target and fsync the
   directory.
6. If anything fails after step 3, the staging file is removed and the
   pre-restore trio is moved back: an absent target stays absent and an
   existing target is byte-for-byte what it was.
"""

from __future__ import annotations

import fcntl
import gzip
import logging
import os
import shutil
import sqlite3
import stat
import struct
import sys
import tempfile
import zlib
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
BACKUP_ROOT = DATA_DIR / "backups"
PRE_RESTORE_SUFFIX = ".pre-restore"
IN_PROGRESS_SUFFIXES = (".partial", ".tmp")

# Byte of the ``-shm`` file on which every open WAL-mode connection holds
# a shared lock for its whole lifetime (``UNIX_SHM_DMS`` in SQLite's
# os_unix.c).  A write lock on it is possible only while no process in
# any container on this host has the database open.  ``F_GETLK`` asks the
# kernel whether such a lock could be taken without taking it, and works
# on a read-only descriptor, so a ``-shm`` created by the root-run trainer
# is checked as well.
_SHM_DMS_OFFSET = 128
# ``struct flock`` on LP64 Linux (x86_64 and aarch64, glibc and musl):
# short l_type, short l_whence, off_t l_start, off_t l_len, pid_t l_pid.
_FLOCK = "hhxxxxqqixxxx"
# Escape hatch for an environment where the lock query itself cannot run;
# never needed on the reference setup.
SKIP_OPEN_CHECK_ENV = "RESTORE_SKIP_OPEN_CHECK"


class RestoreError(RuntimeError):
    """A restore was refused up front or failed and was rolled back."""


def fsync_file(path: Path) -> None:
    """Flush *path*'s data to stable storage."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    """Flush *path*'s directory entries (renames, unlinks) to stable storage."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def live_paths(data_dir: Path, db_name: str) -> tuple[Path, Path, Path]:
    """Return the database file and its WAL and SHM sidecars."""
    target = data_dir / f"{db_name}.db"
    return target, Path(f"{target}-wal"), Path(f"{target}-shm")


def pre_restore_path(path: Path) -> Path:
    return Path(f"{path}{PRE_RESTORE_SUFFIX}")


def database_in_use(target: Path) -> bool:
    """True when some process still has *target* open in WAL mode.

    A database last used in rollback-journal mode has no ``-shm`` file
    and its idle connections hold no lock, so only WAL users (every
    ScarGuard service) are detectable.  Raises :class:`RestoreError` if
    the check cannot be performed, unless ``RESTORE_SKIP_OPEN_CHECK=1``."""
    shm = Path(f"{target}-shm")
    if not shm.exists():
        return False
    if os.environ.get(SKIP_OPEN_CHECK_ENV) == "1":
        logger.warning("%s=1: not checking whether %s is open", SKIP_OPEN_CHECK_ENV, target)
        return False
    try:
        fd = os.open(shm, os.O_RDONLY)
    except OSError as exc:
        raise RestoreError(
            f"cannot open {shm} to check for open connections ({exc}); fix its "
            f"ownership, or set {SKIP_OPEN_CHECK_ENV}=1 once every service is stopped",
        ) from exc
    try:
        probe = struct.pack(_FLOCK, fcntl.F_WRLCK, os.SEEK_SET, _SHM_DMS_OFFSET, 1, 0)
        try:
            answer = fcntl.fcntl(fd, fcntl.F_GETLK, probe)
        except OSError as exc:
            raise RestoreError(
                f"lock query on {shm} failed ({exc}); set {SKIP_OPEN_CHECK_ENV}=1 "
                "once every service is stopped",
            ) from exc
    finally:
        os.close(fd)
    return bool(struct.unpack(_FLOCK, answer)[0] != fcntl.F_UNLCK)


def validate_snapshot(path: Path) -> None:
    """``PRAGMA quick_check`` *path* without creating WAL/SHM sidecars.

    ``immutable=1`` makes SQLite read the file as-is: no locks, no
    journal, nothing written next to it."""
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise RestoreError(f"{path.name} is not a readable SQLite database: {exc}") from exc
    if rows != [("ok",)]:
        problems = "; ".join(str(r[0]) for r in rows[:3])
        raise RestoreError(f"{path.name} failed quick_check: {problems}")


def _available_snapshots(db_dir: Path) -> list[str]:
    if not db_dir.is_dir():
        return []
    return sorted(
        p.name
        for p in db_dir.glob("*.db*")
        if p.is_file() and not p.name.endswith(IN_PROGRESS_SUFFIXES)
    )


def _unpack(source: Path, staged: Path, fd: int) -> None:
    """Write the (possibly gzipped) snapshot into the staging file."""
    try:
        with os.fdopen(fd, "wb") as f_out:
            if source.name.endswith(".gz"):
                with gzip.open(source, "rb") as f_in:
                    shutil.copyfileobj(f_in, f_out)
            else:
                with source.open("rb") as f_in:
                    shutil.copyfileobj(f_in, f_out)
    except (OSError, zlib.error) as exc:  # gzip.BadGzipFile is an OSError
        raise RestoreError(f"could not unpack {source.name} into {staged.name}: {exc}") from exc
    except EOFError as exc:
        raise RestoreError(f"{source.name} is truncated: {exc}") from exc


def do_restore(
    db_name: str,
    backup_filename: str,
    data_dir: Path = DATA_DIR,
    backup_root: Path = BACKUP_ROOT,
) -> None:
    """Replace ``<data_dir>/<db_name>.db`` with the named snapshot.

    Raises :class:`RestoreError` if the restore is refused or if it
    failed and the previous state was put back."""
    for value, what in ((db_name, "database name"), (backup_filename, "backup filename")):
        if not value or Path(value).name != value or value in (".", ".."):
            raise RestoreError(f"invalid {what}: {value!r}")
    if backup_filename.endswith(IN_PROGRESS_SUFFIXES):
        raise RestoreError(f"{backup_filename} is an in-progress snapshot, not a backup")

    db_dir = backup_root / db_name
    source = db_dir / backup_filename
    if not source.is_file():
        available = ", ".join(_available_snapshots(db_dir)) or "(none)"
        raise RestoreError(f"backup {source} not found; available for {db_name}: {available}")

    target, wal, shm = live_paths(data_dir, db_name)
    live = (target, wal, shm)
    leftovers = [pre_restore_path(p) for p in live if pre_restore_path(p).exists()]
    if leftovers:
        names = ", ".join(p.name for p in leftovers)
        raise RestoreError(
            f"rollback copy from an earlier restore is still present ({names}); "
            "verify that restore and remove it, or move it back, before restoring again",
        )
    in_use_message = (
        f"{target} is still open by another process; stop every service that uses it first"
    )
    if database_in_use(target):
        raise RestoreError(in_use_message)

    # Staging files left by an interrupted earlier run of this very command.
    for stale in data_dir.glob(f".{db_name}.db.restore-*.tmp"):
        logger.warning("Removing stale staging file %s", stale.name)
        stale.unlink()

    # Stage and validate first: a bad snapshot never touches the live files.
    fd, staged_name = tempfile.mkstemp(
        dir=data_dir, prefix=f".{db_name}.db.restore-", suffix=".tmp"
    )
    staged = Path(staged_name)
    try:
        _unpack(source, staged, fd)
        validate_snapshot(staged)
        # mkstemp creates 0600; keep the mode the services expect.
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
        os.chmod(staged, mode)
        fsync_file(staged)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    logger.info("Snapshot %s unpacked to %s and passed quick_check", source.name, staged.name)

    moved: list[tuple[Path, Path]] = []
    target_existed = target.exists()
    replaced = False
    try:
        # Unpacking can take a while; make sure nothing opened the database meanwhile.
        if database_in_use(target):
            raise RestoreError(in_use_message)
        for path in live:
            if os.path.lexists(path):
                aside = pre_restore_path(path)
                os.replace(path, aside)
                moved.append((path, aside))
        os.replace(staged, target)
        replaced = True
        fsync_dir(data_dir)
    except BaseException as exc:
        _rollback(data_dir, target, staged, moved, replaced and not target_existed)
        raise RestoreError(f"restore of {db_name} failed and was rolled back: {exc}") from exc

    kept = ", ".join(aside.name for _, aside in moved) or "(nothing: target was absent)"
    logger.info("Restored %s from %s; rollback copy: %s", target, source.name, kept)


def _rollback(
    data_dir: Path,
    target: Path,
    staged: Path,
    moved: list[tuple[Path, Path]],
    remove_target: bool,
) -> None:
    """Put the live files back exactly as they were before the swap.

    Every step is attempted even if an earlier one fails; what could not
    be put back is reported in the raised :class:`RestoreError` so the
    operator knows exactly which ``.pre-restore`` files still hold the
    previous state."""
    stuck: list[str] = []
    try:
        staged.unlink(missing_ok=True)
    except OSError:
        logger.exception("Could not remove staging file %s", staged)
    if remove_target:
        # The staging file reached the target but there was no original;
        # leave the target absent as it was.
        try:
            target.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove %s during rollback", target)
            stuck.append(f"{target.name} (restored content, should be absent)")
    for path, aside in reversed(moved):
        try:
            os.replace(aside, path)
        except OSError:
            logger.exception("Could not move %s back to %s", aside, path)
            stuck.append(f"{aside.name} -> {path.name}")
    try:
        fsync_dir(data_dir)
    except OSError:
        logger.exception("fsync of %s failed during rollback", data_dir)
    if stuck:
        raise RestoreError("rollback incomplete; put back by hand: " + "; ".join(stuck))


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    if len(argv) != 3:
        print("Usage: python restore.py <db_name> <backup_filename>", file=sys.stderr)
        return 2
    try:
        do_restore(argv[1], argv[2])
    except RestoreError as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
