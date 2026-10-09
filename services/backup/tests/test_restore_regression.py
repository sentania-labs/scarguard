"""Regression tests for FDY-0600: recoverable restore and daily retention.

Every test drives a real artifact: ``services/backup/src/restore.py``
(as a module and as the CLI the restore script runs), the sidecar's
``backup_database`` / ``prune_backups`` / ``run_backup_cycle``, the web
``routes/backups.py`` listing, and ``scripts/restore-from-backup.sh``
run against a stand-in ``docker`` on PATH.

On the pre-fix tree the restore module does not exist, retention counts
files instead of calendar days, an overlapping cycle is dropped, the web
listing shows in-progress files, and the shell script leaves the stack
stopped after a failed restore - so these assertions fail there.

CI also runs this file inside the backup image (``/app/tests``), where
only ``/app/src`` exists: the tests that need the web source or the host
script skip there and run from a repository checkout.
"""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest


def _find_checkout() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "scripts" / "restore-from-backup.sh").is_file():
            return parent
    return None


REPO_ROOT = _find_checkout()


def _checkout() -> Path:
    if REPO_ROOT is None:
        pytest.skip("needs a repository checkout (not available inside the backup image)")
    return REPO_ROOT


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def restore_mod() -> ModuleType:
    import restore
    return restore


@pytest.fixture()
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Disposable data dir + backup root, bound into the sidecar module."""
    data_dir = tmp_path / "data"
    backup_root = data_dir / "backups"
    backup_root.mkdir(parents=True)
    import main as backup_main
    monkeypatch.setattr(backup_main, "DATA_DIR", data_dir)
    monkeypatch.setattr(backup_main, "BACKUP_ROOT", backup_root)
    return data_dir, backup_root


def _make_db(path: Path, table: str, rows: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany(f"INSERT INTO {table} (v) VALUES (?)", [(f"r{i}",) for i in range(rows)])
    conn.commit()
    conn.close()


def _make_stale_wal_db(path: Path, rows: int = 50) -> tuple[bytes, bytes]:
    """Leave *path* as a crashed WAL writer would: the main file has no
    table yet, every committed row lives only in ``path-wal``.

    Returns the (db, wal) bytes so tests can check byte-for-byte rollback."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"old{i}",) for i in range(rows)])
    conn.commit()
    wal = Path(f"{path}-wal")
    db_bytes, wal_bytes = path.read_bytes(), wal.read_bytes()
    conn.close()  # checkpoints and deletes the WAL ...
    path.write_bytes(db_bytes)  # ... so put the pre-checkpoint state back
    wal.write_bytes(wal_bytes)
    Path(f"{path}-shm").write_bytes(b"\0" * 32768)
    return db_bytes, wal_bytes


def _count(path: Path, table: str) -> int:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _snapshot_from_sidecar(dirs: tuple[Path, Path], table: str, rows: int) -> str:
    """Produce a real sidecar snapshot of a database holding *table*."""
    import main as backup_main
    data_dir, _ = dirs
    src = data_dir / f"src-{table}.db"
    _make_db(src, table, rows)
    out = backup_main.backup_database("scarguard", src, compress=True)
    assert out is not None
    src.unlink()
    return out.name


def _data_entries(data_dir: Path) -> set[str]:
    return {p.name for p in data_dir.iterdir()}


# ── restore path ─────────────────────────────────────────────────────────


def test_restore_swaps_in_snapshot_without_replaying_stale_wal(
    restore_mod: ModuleType, dirs: tuple[Path, Path],
) -> None:
    data_dir, backup_root = dirs
    target = data_dir / "scarguard.db"
    db_bytes, wal_bytes = _make_stale_wal_db(target)
    target.chmod(0o664)

    # Sanity: the stale WAL is live - a normal open would replay it.
    probe = data_dir / "probe.db"
    probe.write_bytes(db_bytes)
    Path(f"{probe}-wal").write_bytes(wal_bytes)
    conn = sqlite3.connect(probe)
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 50
    conn.close()
    for p in data_dir.glob("probe.db*"):
        p.unlink()

    snapshot = _snapshot_from_sidecar(dirs, "clean_t", 3)

    restore_mod.do_restore("scarguard", snapshot, data_dir, backup_root)

    # The restored file is the snapshot, with nothing from the old WAL.
    assert _tables(target) == {"clean_t"}
    assert _count(target, "clean_t") == 3
    assert stat.S_IMODE(target.stat().st_mode) == 0o664, "mode of the live file is kept"
    assert not Path(f"{target}-wal").exists()
    assert not Path(f"{target}-shm").exists()
    conn = sqlite3.connect(target)  # a normal open finds no WAL to replay
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master")} == {"clean_t"}
    conn.close()

    # The rollback copy is the complete, coherent previous state.
    assert (data_dir / "scarguard.db.pre-restore").read_bytes() == db_bytes
    assert (data_dir / "scarguard.db-wal.pre-restore").read_bytes() == wal_bytes
    assert (data_dir / "scarguard.db-shm.pre-restore").exists()
    rollback = data_dir / "rollback-check.db"
    rollback.write_bytes((data_dir / "scarguard.db.pre-restore").read_bytes())
    Path(f"{rollback}-wal").write_bytes((data_dir / "scarguard.db-wal.pre-restore").read_bytes())
    conn = sqlite3.connect(rollback)
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 50
    conn.close()

    # No staging leftovers.
    assert not [p for p in data_dir.iterdir() if ".restore-" in p.name]


def test_restore_rejects_bad_snapshots_and_leaves_absent_target_absent(
    restore_mod: ModuleType, dirs: tuple[Path, Path],
) -> None:
    data_dir, backup_root = dirs
    db_dir = backup_root / "scarguard"
    db_dir.mkdir()
    with gzip.open(db_dir / "garbage.db.gz", "wb") as f:
        f.write(b"this is not a sqlite database" * 100)
    good = _snapshot_from_sidecar(dirs, "clean_t", 3)
    good_bytes = (db_dir / good).read_bytes()
    (db_dir / "truncated.db.gz").write_bytes(good_bytes[:-40])
    # Valid gzip header, corrupt deflate stream.
    (db_dir / "bitrot.db.gz").write_bytes(good_bytes[:20] + b"\xff" * 40 + good_bytes[60:])
    (db_dir / "raw-garbage.db").write_bytes(b"\0" * 4096)
    # A staging file an interrupted earlier restore left behind.
    (data_dir / ".scarguard.db.restore-old.tmp").write_bytes(b"leftover")

    for name in ("garbage.db.gz", "truncated.db.gz", "bitrot.db.gz", "raw-garbage.db"):
        with pytest.raises(restore_mod.RestoreError):
            restore_mod.do_restore("scarguard", name, data_dir, backup_root)
        assert _data_entries(data_dir) == {"backups"}, name

    # The absent-target case also works for a good snapshot.
    restore_mod.do_restore("scarguard", good, data_dir, backup_root)
    assert _data_entries(data_dir) == {"backups", "scarguard.db"}
    assert _count(data_dir / "scarguard.db", "clean_t") == 3
    assert stat.S_IMODE((data_dir / "scarguard.db").stat().st_mode) == 0o644


def test_restore_rejects_bad_snapshot_without_touching_existing_target(
    restore_mod: ModuleType, dirs: tuple[Path, Path],
) -> None:
    data_dir, backup_root = dirs
    target = data_dir / "scarguard.db"
    db_bytes, wal_bytes = _make_stale_wal_db(target)
    shm_bytes = Path(f"{target}-shm").read_bytes()
    (backup_root / "scarguard").mkdir()
    with gzip.open(backup_root / "scarguard" / "bad.db.gz", "wb") as f:
        f.write(b"nope" * 2048)

    with pytest.raises(restore_mod.RestoreError, match="not a readable SQLite database"):
        restore_mod.do_restore("scarguard", "bad.db.gz", data_dir, backup_root)

    assert _data_entries(data_dir) == {
        "backups", "scarguard.db", "scarguard.db-wal", "scarguard.db-shm",
    }
    assert target.read_bytes() == db_bytes
    assert Path(f"{target}-wal").read_bytes() == wal_bytes
    assert Path(f"{target}-shm").read_bytes() == shm_bytes


@pytest.mark.parametrize("fault", ["replace_staged", "fsync_dir"])
def test_restore_rolls_back_when_the_swap_itself_fails(
    restore_mod: ModuleType, dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    data_dir, backup_root = dirs
    target = data_dir / "scarguard.db"
    db_bytes, wal_bytes = _make_stale_wal_db(target)
    snapshot = _snapshot_from_sidecar(dirs, "clean_t", 3)

    real_replace = os.replace
    if fault == "replace_staged":
        # Fails the staged -> target rename, after the live files were moved aside.
        def failing_replace(src: Any, dst: Any) -> None:
            if ".restore-" in str(src):
                raise OSError("disk fault during rename")
            real_replace(src, dst)
        monkeypatch.setattr(restore_mod.os, "replace", failing_replace)
    else:
        # Fails after the rename succeeded: the restored file must be undone too.
        def failing_fsync_dir(path: Path) -> None:
            raise OSError("fsync fault after replace")
        monkeypatch.setattr(restore_mod, "fsync_dir", failing_fsync_dir)

    with pytest.raises(restore_mod.RestoreError, match="rolled back"):
        restore_mod.do_restore("scarguard", snapshot, data_dir, backup_root)

    assert _data_entries(data_dir) == {
        "backups", "scarguard.db", "scarguard.db-wal", "scarguard.db-shm",
    }
    assert target.read_bytes() == db_bytes
    assert Path(f"{target}-wal").read_bytes() == wal_bytes

    # Same fault with no original target: the target must stay absent.
    for p in list(data_dir.glob("scarguard.db*")):
        p.unlink()
    with pytest.raises(restore_mod.RestoreError, match="rolled back"):
        restore_mod.do_restore("scarguard", snapshot, data_dir, backup_root)
    assert _data_entries(data_dir) == {"backups"}


def test_restore_refuses_in_progress_missing_and_traversal_names(
    restore_mod: ModuleType, dirs: tuple[Path, Path],
) -> None:
    data_dir, backup_root = dirs
    target = data_dir / "scarguard.db"
    db_bytes, _ = _make_stale_wal_db(target)
    good = _snapshot_from_sidecar(dirs, "clean_t", 3)
    db_dir = backup_root / "scarguard"
    partial = f"{good}.1a2b3c4d.partial"
    shutil.copy(db_dir / good, db_dir / partial)
    tmp = "2026-04-22T08-00-00.abc123.db.tmp"
    shutil.copy(db_dir / good, db_dir / tmp)
    for name in (partial, tmp, "does-not-exist.db.gz", "../outside.db", "", "..", "sub/x.db"):
        with pytest.raises(restore_mod.RestoreError) as info:
            restore_mod.do_restore("scarguard", name, data_dir, backup_root)
        assert target.read_bytes() == db_bytes, name
        if name == "does-not-exist.db.gz":
            # The error lists what is restorable - and only that.
            assert good in str(info.value)
            assert partial not in str(info.value)
            assert tmp not in str(info.value)
    with pytest.raises(restore_mod.RestoreError):
        restore_mod.do_restore("../scarguard", good, data_dir, backup_root)
    assert not [p for p in data_dir.iterdir() if ".restore-" in p.name]


def test_restore_refuses_while_a_service_still_holds_the_database(
    restore_mod: ModuleType, dirs: tuple[Path, Path],
) -> None:
    data_dir, backup_root = dirs
    target = data_dir / "scarguard.db"
    _make_db(target, "t", 5)
    snapshot = _snapshot_from_sidecar(dirs, "clean_t", 3)

    # A separate process (like a running service container) keeps a
    # WAL-mode connection open across the restore attempt.
    holder = subprocess.Popen(
        [sys.executable, "-c", (
            "import sqlite3, sys, time\n"
            f"c = sqlite3.connect({str(target)!r})\n"
            "c.execute('PRAGMA journal_mode=WAL')\n"
            "c.execute('SELECT COUNT(*) FROM t').fetchone()\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n"
        )],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "ready"
        with pytest.raises(restore_mod.RestoreError, match="still open"):
            restore_mod.do_restore("scarguard", snapshot, data_dir, backup_root)
        assert _tables(target) == {"t"}
        # The trainer runs as root: its -shm is not writable by the restore
        # user. The check still works on a read-only descriptor.
        Path(f"{target}-shm").chmod(0o444)
        with pytest.raises(restore_mod.RestoreError, match="still open"):
            restore_mod.do_restore("scarguard", snapshot, data_dir, backup_root)
        Path(f"{target}-shm").chmod(0o644)
    finally:
        holder.kill()
        holder.wait()

    # Once the holder is gone (its lock died with it) the restore proceeds.
    restore_mod.do_restore("scarguard", snapshot, data_dir, backup_root)
    assert _tables(target) == {"clean_t"}


def test_restore_never_overwrites_an_earlier_rollback_copy(
    restore_mod: ModuleType, dirs: tuple[Path, Path],
) -> None:
    data_dir, backup_root = dirs
    target = data_dir / "scarguard.db"
    first = _snapshot_from_sidecar(dirs, "first_t", 1)
    second = _snapshot_from_sidecar(dirs, "second_t", 2)
    _make_db(target, "original", 4)
    original = target.read_bytes()

    restore_mod.do_restore("scarguard", first, data_dir, backup_root)
    assert (data_dir / "scarguard.db.pre-restore").read_bytes() == original

    with pytest.raises(restore_mod.RestoreError, match="earlier restore"):
        restore_mod.do_restore("scarguard", second, data_dir, backup_root)
    assert (data_dir / "scarguard.db.pre-restore").read_bytes() == original
    assert _tables(target) == {"first_t"}

    (data_dir / "scarguard.db.pre-restore").unlink()
    restore_mod.do_restore("scarguard", second, data_dir, backup_root)
    assert _tables(target) == {"second_t"}


def test_restore_cli_is_what_the_script_runs(
    restore_mod: ModuleType, dirs: tuple[Path, Path],
) -> None:
    """Drive restore.py exactly as scripts/restore-from-backup.sh does
    (``python src/restore.py <db> <file>`` with DATA_DIR in the env)."""
    data_dir, _ = dirs
    target = data_dir / "scarguard.db"
    _make_stale_wal_db(target)
    snapshot = _snapshot_from_sidecar(dirs, "clean_t", 3)
    env = {**os.environ, "DATA_DIR": str(data_dir)}
    cmd = [sys.executable, "src/restore.py", "scarguard"]
    workdir = Path(restore_mod.__file__).resolve().parent.parent  # /app or services/backup

    ok = subprocess.run(
        [*cmd, snapshot], cwd=workdir, env=env, capture_output=True, text=True,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "Restored" in ok.stdout
    assert _tables(target) == {"clean_t"}
    assert not Path(f"{target}-wal").exists()

    (data_dir / "scarguard.db.pre-restore").unlink()
    (data_dir / "scarguard.db-wal.pre-restore").unlink()
    (data_dir / "scarguard.db-shm.pre-restore").unlink()
    bad = subprocess.run(
        [*cmd, "missing.db.gz"], cwd=workdir, env=env, capture_output=True, text=True,
    )
    assert bad.returncode == 1
    assert "not found" in bad.stdout and snapshot in bad.stdout
    assert _tables(target) == {"clean_t"}

    usage = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True)
    assert usage.returncode == 2


# ── sidecar: retention, serialization, unique names ──────────────────────


def _seed(db_dir: Path, names: list[str]) -> None:
    db_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (db_dir / name).write_bytes(b"x")


def test_retention_keeps_recovery_points_per_day_not_per_file(dirs: tuple[Path, Path]) -> None:
    import main as backup_main
    _, backup_root = dirs
    today = date(2026, 4, 22)
    d1, d2, d3 = (today - timedelta(days=i) for i in (1, 2, 3))
    # Several same-day snapshots, as left by sidecar restarts.
    _seed(backup_root / "scarguard", [
        f"{today}T10-00-00.db.gz", f"{today}T09-00-00.db.gz", f"{today}T08-00-00.db.gz",
        f"{d1}T09-00-00.db.gz", f"{d1}T08-00-00.db.gz",
        f"{d2}T08-00-00.db.gz",
        f"{d3}T08-00-00.db.gz",
    ])

    deleted = backup_main.prune_backups("scarguard", daily=3, weekly=0)

    # Three calendar days survive. The newest day keeps every snapshot
    # (intra-day points for short intervals); older days collapse to one.
    remaining = sorted(p.name for p in (backup_root / "scarguard").iterdir())
    assert remaining == [
        f"{d2}T08-00-00.db.gz",
        f"{d1}T09-00-00.db.gz",
        f"{today}T08-00-00.db.gz", f"{today}T09-00-00.db.gz", f"{today}T10-00-00.db.gz",
    ]
    assert deleted == 2


def test_retention_removes_only_orphaned_in_progress_files(dirs: tuple[Path, Path]) -> None:
    import main as backup_main
    _, backup_root = dirs
    db_dir = backup_root / "scarguard"
    orphan = "2026-04-20T08-00-00.db.gz.0badf00d.partial"
    fresh = "2026-04-22T08-00-00.a1b2c3.db.tmp"
    _seed(db_dir, ["2026-04-22T08-00-00.db.gz", orphan, fresh])
    old = time.time() - 2 * backup_main.IN_PROGRESS_MAX_AGE_SECONDS
    os.utime(db_dir / orphan, (old, old))

    deleted = backup_main.prune_backups("scarguard", daily=14, weekly=8)

    assert deleted == 1
    assert {p.name for p in db_dir.iterdir()} == {"2026-04-22T08-00-00.db.gz", fresh}


def test_manual_backups_and_daily_points_are_retained_independently(
    dirs: tuple[Path, Path],
) -> None:
    import main as backup_main
    _, backup_root = dirs
    today = date(2026, 4, 22)
    scheduled = [f"{today - timedelta(days=i)}T08-00-00.db.gz" for i in range(14)]
    manual = [f"{today}T{10 + i:02d}-00-00-manual.db.gz" for i in range(12)]
    partial = f"{today}T23-00-00.db.gz.0badf00d.partial"
    foreign = ["pre-upgrade-copy.db.gz", "2026-04-01-copied-by-hand.db.gz"]
    _seed(backup_root / "auth", [*scheduled, *manual, partial, *foreign])

    backup_main.prune_backups("auth", daily=14, weekly=0)

    remaining = {p.name for p in (backup_root / "auth").iterdir()}
    assert set(scheduled) <= remaining, "manual backups must not evict daily recovery points"
    kept_manual = sorted(n for n in remaining if "-manual" in n)
    assert kept_manual == sorted(manual)[-backup_main.MANUAL_RETENTION:]
    assert partial in remaining and set(foreign) <= remaining  # never touched

    # And the other way round: a flood of daily points never evicts manual ones.
    backup_main.prune_backups("auth", daily=1, weekly=0)
    remaining = {p.name for p in (backup_root / "auth").iterdir()}
    assert sorted(n for n in remaining if "-manual" in n) == kept_manual
    assert [n for n in remaining if n in scheduled] == [scheduled[0]]


def test_overlapping_cycle_waits_its_turn_instead_of_being_dropped(
    dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main as backup_main
    data_dir, backup_root = dirs
    _make_db(data_dir / "scarguard.db", "t", 2)
    monkeypatch.setattr(backup_main, "DATABASES", (("scarguard", data_dir / "scarguard.db"),))
    publisher = MagicMock()
    cfg = {"compress": True, "retention_daily": 14, "retention_weekly": 8}
    results: list[dict[str, Any]] = []

    # The scheduled cycle is "running": it holds the lock.
    backup_main.backup_lock.acquire()
    try:
        worker = threading.Thread(
            target=lambda: results.append(
                backup_main.run_backup_cycle(cfg, publisher, triggered_by="manual"),
            ),
        )
        worker.start()
        time.sleep(0.3)
        assert worker.is_alive(), "manual cycle must wait, not return"
        assert results == []
        published = [c.args[1] for c in publisher.publish.call_args_list]
        assert any('"queued"' in p and '"manual"' in p for p in published)
        assert not (backup_root / "scarguard").exists(), "nothing is written while waiting"

        # A second manual trigger while one is already waiting is redundant:
        # it is reported as skipped right away instead of piling up.
        extra = backup_main.run_backup_cycle(cfg, publisher, triggered_by="manual")
        assert extra["phase"] == "skipped"
        assert worker.is_alive() and results == []
    finally:
        backup_main.backup_lock.release()
    worker.join(timeout=10)
    assert not worker.is_alive()

    assert results and results[0]["phase"] == "completed" and results[0]["success"] is True
    files = list((backup_root / "scarguard").glob("*.db*"))
    assert len(files) == 1 and "-manual" in files[0].name
    assert not backup_main.backup_lock.locked()

    # Nothing is running any more: a new manual trigger runs immediately.
    again = backup_main.run_backup_cycle(cfg, publisher, triggered_by="manual")
    assert again["phase"] == "completed"
    assert len(list((backup_root / "scarguard").glob("*.db*"))) == 2


def test_same_second_snapshots_get_unique_names_and_leave_no_temp_files(
    dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main as backup_main
    data_dir, backup_root = dirs
    src = data_dir / "scarguard.db"
    _make_db(src, "t", 3)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "FrozenDatetime":
            return cls(2026, 4, 22, 8, 0, 0, tzinfo=tz)

    monkeypatch.setattr(backup_main, "datetime", FrozenDatetime)
    first = backup_main.backup_database("scarguard", src, compress=True)
    second = backup_main.backup_database("scarguard", src, compress=True, triggered_by="manual")
    third = backup_main.backup_database("scarguard", src, compress=True, triggered_by="manual")
    assert first is not None and second is not None and third is not None
    names = {first.name, second.name, third.name}
    assert len(names) == 3
    assert first.name == "2026-04-22T08-00-00.db.gz"
    assert second.name == "2026-04-22T08-00-00-manual.db.gz"
    assert third.name.startswith("2026-04-22T08-00-00-manual-") and third.name.endswith(".db.gz")
    assert {p.name for p in (backup_root / "scarguard").iterdir()} == names

    for path in (first, second, third):
        with gzip.open(path, "rb") as f:
            payload = f.read()
        check = data_dir / "check.db"
        check.write_bytes(payload)
        assert _count(check, "t") == 3


def test_corrupt_source_leaves_no_snapshot_or_temp_file(dirs: tuple[Path, Path]) -> None:
    import main as backup_main
    data_dir, backup_root = dirs
    src = data_dir / "scarguard.db"
    src.write_bytes(b"not a database" * 512)
    with pytest.raises(sqlite3.DatabaseError):
        backup_main.backup_database("scarguard", src, compress=True)
    assert list((backup_root / "scarguard").iterdir()) == []


# ── web route: in-progress files are never listed or served ──────────────


@pytest.fixture()
def web_backups_route(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """Import the real services/web/src/routes/backups.py.

    The backup test environment has no FastAPI, so the framework and the
    web-service-local helper modules are stubbed; the listing/resolve
    code under test is pure filesystem logic."""
    for name in (
        "fastapi", "fastapi.responses", "fastapi.templating", "starlette",
        "starlette.responses", "audit", "config_store", "rate_limit_dep",
        "route_auth", "sse_limiter",
    ):
        monkeypatch.setitem(sys.modules, name, MagicMock())
    for name in [m for m in sys.modules if m == "routes" or m.startswith("routes.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(_checkout() / "services" / "web" / "src"))
    from routes import backups as backups_route
    yield backups_route
    for name in [m for m in sys.modules if m == "routes" or m.startswith("routes.")]:
        del sys.modules[name]


def test_web_listing_and_download_skip_in_progress_files(
    web_backups_route: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_root = tmp_path / "backups"
    assert web_backups_route.__file__.endswith("routes/backups.py")
    monkeypatch.setattr(web_backups_route, "BACKUP_ROOT", backup_root)
    db_dir = backup_root / "scarguard"
    db_dir.mkdir(parents=True)
    finished = "2026-04-22T08-00-00.db.gz"
    manual = "2026-04-22T09-00-00-manual.db.gz"
    in_progress = [
        "2026-04-22T10-00-00.db.gz.1a2b3c4d.partial",  # gzip staging (current naming)
        "2026-04-22T10-00-00.a1b2c3.db.tmp",  # online-backup staging (current naming)
        "2026-04-21T08-00-00.db.gz.partial",  # leftover from a pre-fix sidecar
    ]
    for name in (finished, manual, *in_progress):
        (db_dir / name).write_bytes(b"x")

    listed = [b["filename"] for b in web_backups_route._list_backups()]
    assert sorted(listed) == sorted([finished, manual])

    assert web_backups_route._safe_resolve("scarguard", finished) == (db_dir / finished).resolve()
    for name in in_progress:
        assert web_backups_route._safe_resolve("scarguard", name) is None, name


# ── host script: services come back even when the restore fails ──────────


def _docker_shim(tmp_path: Path, run_rc: int) -> tuple[Path, Path]:
    """A ``docker`` on PATH that records compose calls and fakes a stack
    where redis, web, detector and backup are running."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"$*\" >> '{log}'\n"
        "case \"$*\" in\n"
        "  *' ps --services --status running'*) printf 'redis\\nweb\\ndetector\\nbackup\\n' ;;\n"
        f"  *' run '*) exit {run_rc} ;;\n"
        "esac\n"
        "exit 0\n",
    )
    shim.chmod(0o755)
    return bin_dir, log


def _run_script(bin_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    repo = _checkout()
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", str(repo / "scripts" / "restore-from-backup.sh"), *args],
        cwd=repo, env=env, capture_output=True, text=True,
    )


def test_restore_script_restarts_stopped_services_when_restore_fails(tmp_path: Path) -> None:
    bin_dir, log = _docker_shim(tmp_path, run_rc=1)

    result = _run_script(bin_dir, "scarguard", "2026-04-22T08-00-00.db.gz")

    assert result.returncode == 1, result.stdout + result.stderr
    calls = log.read_text().splitlines()
    assert calls == [
        "compose --profile training ps --services --status running",
        "compose --profile training stop detector web backup",
        "compose --profile training run --rm --no-deps --entrypoint python backup "
        "src/restore.py scarguard 2026-04-22T08-00-00.db.gz",
        "compose --profile training start detector web backup",
    ]
    assert "did not complete" in result.stderr


def test_restore_script_only_touches_services_that_were_running(tmp_path: Path) -> None:
    bin_dir, log = _docker_shim(tmp_path, run_rc=0)

    result = _run_script(bin_dir, "deterrent", "2026-04-22T08-00-00.db.gz")

    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text().splitlines()
    # deterrent itself is not running in the fake stack: it is neither
    # stopped nor started; trainer is never named for deterrent.db.
    assert calls == [
        "compose --profile training ps --services --status running",
        "compose --profile training stop web backup",
        "compose --profile training run --rm --no-deps --entrypoint python backup "
        "src/restore.py deterrent 2026-04-22T08-00-00.db.gz",
        "compose --profile training start web backup",
    ]
    assert "pre-restore" in result.stdout

    bad = _run_script(bin_dir, "nope", "x.db.gz")
    assert bad.returncode == 2
    assert log.read_text().splitlines() == calls  # nothing else was called
