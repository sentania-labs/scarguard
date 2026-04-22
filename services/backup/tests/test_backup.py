"""Tests for the SQLite backup sidecar.

Exercises the backup, prune, and rotation logic with real (in-memory and
on-disk) SQLite databases. Doesn't touch Redis — that's tested via the
integration smoke test."""

from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Set DATA_DIR and BACKUP_ROOT in the module to a tmp path."""
    data_dir = tmp_path / "data"
    backup_root = data_dir / "backups"
    data_dir.mkdir()
    backup_root.mkdir()
    import main as backup_main
    monkeypatch.setattr(backup_main, "DATA_DIR", data_dir)
    monkeypatch.setattr(backup_main, "BACKUP_ROOT", backup_root)
    return data_dir, backup_root


def _make_db(path: Path, rows: int = 5) -> None:
    """Create a tiny SQLite DB at *path*."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    for i in range(rows):
        conn.execute("INSERT INTO t (v) VALUES (?)", (f"row-{i}",))
    conn.commit()
    conn.close()


class TestBackupDatabase:
    def test_creates_backup_when_source_exists(self, isolated_dirs) -> None:
        import main as backup_main
        data_dir, backup_root = isolated_dirs
        src = data_dir / "scarguard.db"
        _make_db(src, rows=10)
        result = backup_main.backup_database(
            "scarguard", src, compress=True,
        )
        assert result is not None
        assert result.exists()
        assert result.suffix == ".gz"
        assert result.parent.name == "scarguard"

    def test_returns_none_when_source_missing(self, isolated_dirs) -> None:
        import main as backup_main
        data_dir, _ = isolated_dirs
        result = backup_main.backup_database(
            "ghost", data_dir / "ghost.db", compress=True,
        )
        assert result is None

    def test_compressed_backup_round_trips(self, isolated_dirs) -> None:
        import main as backup_main
        data_dir, _ = isolated_dirs
        src = data_dir / "scarguard.db"
        _make_db(src, rows=5)

        out = backup_main.backup_database("scarguard", src, compress=True)
        assert out is not None and out.suffix == ".gz"

        # Gunzip and verify the SQLite payload is intact.
        decompressed = data_dir / "restored.db"
        with gzip.open(out, "rb") as f_in:
            decompressed.write_bytes(f_in.read())

        conn = sqlite3.connect(decompressed)
        rows = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        conn.close()
        assert rows == 5

    def test_uncompressed_backup_round_trips(self, isolated_dirs) -> None:
        import main as backup_main
        data_dir, _ = isolated_dirs
        src = data_dir / "scarguard.db"
        _make_db(src, rows=3)

        out = backup_main.backup_database("scarguard", src, compress=False)
        assert out is not None and out.suffix == ".db"

        conn = sqlite3.connect(out)
        rows = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        conn.close()
        assert rows == 3

    def test_creates_dated_filename(self, isolated_dirs) -> None:
        import main as backup_main
        data_dir, _ = isolated_dirs
        src = data_dir / "scarguard.db"
        _make_db(src)
        out = backup_main.backup_database("scarguard", src, compress=True)
        assert out is not None
        # Filename starts with YYYY-MM-DDTHH-MM-SS
        import re
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}", out.name)


class TestPruneBackups:
    def _seed(self, root: Path, db_name: str, dates: list[str]) -> None:
        """Create empty backup files with the given date prefixes."""
        d = root / db_name
        d.mkdir(parents=True, exist_ok=True)
        for date in dates:
            (d / f"{date}T08-00-00.db.gz").write_bytes(b"x")

    def test_keeps_n_most_recent(self, isolated_dirs) -> None:
        import main as backup_main
        _, backup_root = isolated_dirs
        self._seed(backup_root, "scarguard", [
            "2026-04-22", "2026-04-21", "2026-04-20", "2026-04-19", "2026-04-18",
        ])
        deleted = backup_main.prune_backups("scarguard", daily=3, weekly=0)
        assert deleted == 2
        remaining = sorted((backup_root / "scarguard").iterdir())
        names = [f.name[:10] for f in remaining]
        assert names == ["2026-04-20", "2026-04-21", "2026-04-22"]

    def test_keeps_weekly_samples_beyond_dailies(self, isolated_dirs) -> None:
        import main as backup_main
        _, backup_root = isolated_dirs
        # 30 daily files; daily=7 + weekly=3 should keep 7 + 3 = 10.
        dates = []
        for i in range(30):
            from datetime import date, timedelta
            dates.append((date(2026, 4, 22) - timedelta(days=i)).isoformat())
        self._seed(backup_root, "auth", dates)
        backup_main.prune_backups("auth", daily=7, weekly=3)
        remaining = sorted((backup_root / "auth").iterdir())
        # At minimum 7 dailies + some weekly samples; cap at 10.
        assert 7 < len(remaining) <= 10

    def test_returns_zero_when_no_backups(self, isolated_dirs) -> None:
        import main as backup_main
        deleted = backup_main.prune_backups("nonexistent", daily=14, weekly=8)
        assert deleted == 0


class TestRunBackupCycle:
    def test_publishes_started_and_completed(self, isolated_dirs) -> None:
        import main as backup_main
        data_dir, _ = isolated_dirs
        # Seed all three DBs.
        _make_db(data_dir / "scarguard.db", rows=2)
        _make_db(data_dir / "auth.db", rows=2)
        _make_db(data_dir / "deterrent.db", rows=2)
        # Re-bind DATABASES on the freshly-isolated paths
        import importlib
        importlib.reload(backup_main)
        backup_main.DATA_DIR = data_dir
        backup_main.BACKUP_ROOT = data_dir / "backups"
        backup_main.DATABASES = (
            ("scarguard", data_dir / "scarguard.db"),
            ("auth", data_dir / "auth.db"),
            ("deterrent", data_dir / "deterrent.db"),
        )

        publisher = MagicMock()
        result = backup_main.run_backup_cycle(
            {"compress": True, "retention_daily": 14, "retention_weekly": 8},
            publisher,
        )
        assert result["success"] is True
        assert len(result["results"]) == 3
        # Two publishes: started + completed
        assert publisher.publish.call_count == 2


    # Path-traversal coverage for the web download route lives in the
    # web tests (services/web/tests/test_db_backups_route.py); the
    # download surface is in routes/backups.py, not in this sidecar.
