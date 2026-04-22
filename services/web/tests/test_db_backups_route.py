"""Tests for the v1.14 /admin/db-backups route — focused on the
path-traversal-rejecting download endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest


class TestSafeResolve:
    """Direct unit tests on the helper that gates the download path."""

    def test_rejects_path_traversal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from routes import backups as backups_route
        monkeypatch.setattr(backups_route, "BACKUP_ROOT", tmp_path)
        (tmp_path / "scarguard").mkdir()
        (tmp_path / "scarguard" / "ok.db.gz").write_bytes(b"x")

        # Valid
        assert backups_route._safe_resolve("scarguard", "ok.db.gz") is not None

        # Path-traversal attempts — every one of these must return None.
        assert backups_route._safe_resolve("..", "etc/passwd") is None
        assert backups_route._safe_resolve("/etc", "passwd") is None
        assert backups_route._safe_resolve("scarguard", "../../etc/passwd") is None
        assert backups_route._safe_resolve("", "") is None
        # Characters outside the safe allowlist
        assert backups_route._safe_resolve("scarguard", "ok;rm.db") is None

        # Unknown filename (not in the listing)
        assert backups_route._safe_resolve("scarguard", "missing.db") is None

    def test_accepts_real_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from routes import backups as backups_route
        monkeypatch.setattr(backups_route, "BACKUP_ROOT", tmp_path)
        (tmp_path / "auth").mkdir()
        target = tmp_path / "auth" / "2026-04-22T08-00-00.db.gz"
        target.write_bytes(b"\x1f\x8b\x08")  # gzip magic
        resolved = backups_route._safe_resolve("auth", "2026-04-22T08-00-00.db.gz")
        assert resolved == target.resolve()


class TestListBackups:
    def test_empty_directory_returns_empty_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from routes import backups as backups_route
        monkeypatch.setattr(backups_route, "BACKUP_ROOT", tmp_path)
        assert backups_route._list_backups() == []

    def test_returns_per_db_files_sorted_newest_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from routes import backups as backups_route
        monkeypatch.setattr(backups_route, "BACKUP_ROOT", tmp_path)

        (tmp_path / "scarguard").mkdir()
        (tmp_path / "auth").mkdir()
        # Older
        (tmp_path / "scarguard" / "2026-04-20T08-00-00.db.gz").write_bytes(b"x")
        # Newer
        (tmp_path / "auth" / "2026-04-22T08-00-00.db.gz").write_bytes(b"yy")

        result = backups_route._list_backups()
        assert len(result) == 2
        # Newest-first ordering
        assert result[0]["filename"] == "2026-04-22T08-00-00.db.gz"
        assert result[1]["filename"] == "2026-04-20T08-00-00.db.gz"
        # Per-entry fields
        assert result[0]["db"] == "auth"
        assert result[0]["rel_path"] == "auth/2026-04-22T08-00-00.db.gz"
        assert result[0]["size_bytes"] == 2

    def test_skips_non_directory_top_level_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from routes import backups as backups_route
        monkeypatch.setattr(backups_route, "BACKUP_ROOT", tmp_path)
        # A bogus file at the top of BACKUP_ROOT shouldn't be listed.
        (tmp_path / "stray-file").write_bytes(b"x")
        assert backups_route._list_backups() == []


class TestHumanSize:
    @pytest.mark.parametrize("n,expected_unit", [
        (500, "B"),
        (5000, "KB"),
        (5_000_000, "MB"),
        (5_000_000_000, "GB"),
    ])
    def test_units_picked(self, n: int, expected_unit: str) -> None:
        from routes import backups as backups_route
        result = backups_route._human_size(n)
        assert expected_unit in result
