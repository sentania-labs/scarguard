import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_web_route_excludes_partials(monkeypatch, tmp_path):
    """The web db_backups route should exclude partial and temp files."""
    # Since we can't easily import routes.backups due to its dependencies in the test environment,
    # wait, services/web/tests imports it just fine. Let's try importing it.
    import sys
    sys.modules["cryptography"] = MagicMock()
    sys.modules["cryptography.fernet"] = MagicMock()
    sys.modules["fastapi"] = MagicMock()
    sys.modules["fastapi.responses"] = MagicMock()
    sys.modules["fastapi.templating"] = MagicMock()
    sys.modules["starlette"] = MagicMock()
    sys.modules["starlette.responses"] = MagicMock()
    sys.modules["redis"] = MagicMock()
    sys.modules["redis.asyncio"] = MagicMock()
    sys.modules["rate_limit_dep"] = MagicMock()
    sys.modules["route_auth"] = MagicMock()
    sys.modules["sse_limiter"] = MagicMock()
    sys.modules["audit"] = MagicMock()
    sys.modules["config_store"] = MagicMock()

    sys.path.insert(0, str(Path("services/web/src").absolute()))
    from routes import backups as backups_route

    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    monkeypatch.setattr(backups_route, "BACKUP_ROOT", backup_root)

    db_dir = backup_root / "scarguard"
    db_dir.mkdir()

    # Create a valid backup
    (db_dir / "2026-04-22T08-00-00.db.gz").write_bytes(b"valid")
    # Create a partial backup
    (db_dir / "2026-04-22T09-00-00.db.gz.partial").write_bytes(b"partial")
    # Create a tmp backup
    (db_dir / "tmp_abc123.tmp").write_bytes(b"tmp")

    backups = backups_route._list_backups()
    filenames = [b["filename"] for b in backups]

    assert "2026-04-22T08-00-00.db.gz" in filenames
    assert "2026-04-22T09-00-00.db.gz.partial" not in filenames
    assert "tmp_abc123.tmp" not in filenames


def test_restore_clears_stale_wal_and_handles_failures(monkeypatch, tmp_path):
    """Restore should clear stale WAL/SHM, and completely rollback on validation failure."""
    import sys
    sys.path.insert(0, str(Path("services/backup/src").absolute()))
    try:
        import restore
    except ImportError:
        pytest.fail("restore.py not implemented yet")

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    backup_root = data_dir / "backups"
    backup_root.mkdir()
    db_dir = backup_root / "scarguard"
    db_dir.mkdir()

    target_db = data_dir / "scarguard.db"

    # 1. Create a target DB with WAL
    conn = sqlite3.connect(target_db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('original')")
    conn.commit()
    conn.close()

    # Assert WAL exists by touching it if it doesn't
    (data_dir / "scarguard.db-wal").write_bytes(b"")
    assert (data_dir / "scarguard.db-wal").exists()

    # 2. Create a clean backup DB without the table 't'
    backup_db = db_dir / "clean.db"
    b_conn = sqlite3.connect(backup_db)
    b_conn.execute("CREATE TABLE clean_t (id INTEGER PRIMARY KEY)")
    b_conn.commit()
    b_conn.close()

    # 3. Perform restore
    restore.do_restore("scarguard", "clean.db", data_dir, backup_root)

    # 4. Verify WAL is gone and DB is the restored one
    assert not (data_dir / "scarguard.db-wal").exists(), "Stale WAL must be cleared"

    conn = sqlite3.connect(target_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT * FROM t").fetchall()  # Should not exist
    assert len(conn.execute("SELECT * FROM clean_t").fetchall()) == 0
    conn.close()

    # 5. Test rollback on failure when original target was absent
    target_db.unlink()
    # Create corrupt backup
    corrupt_backup = db_dir / "corrupt.db"
    corrupt_backup.write_bytes(b"this is not a sqlite db")

    with pytest.raises(Exception, match="Restore failed.*"):
        restore.do_restore("scarguard", "corrupt.db", data_dir, backup_root)

    assert not target_db.exists(), "Target DB should remain absent after failed restore"
    assert not (data_dir / "scarguard.db-wal").exists()
    assert not (data_dir / "scarguard.db.pre-restore").exists()
