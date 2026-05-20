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


class TestAuthReauthGate:
    """GET /download/auth/* must be blocked; POST with valid password must work."""

    def test_get_auth_download_returns_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GET requests for auth db backups are rejected with 403."""
        from routes import backups as backups_route
        monkeypatch.setattr(backups_route, "BACKUP_ROOT", tmp_path)
        (tmp_path / "auth").mkdir()
        (tmp_path / "auth" / "test.db.gz").write_bytes(b"x")

        from unittest.mock import MagicMock
        from fastapi import HTTPException

        request = MagicMock()
        request.state.user = {"user_id": 1, "username": "admin", "role": "admin", "is_admin": 1}
        monkeypatch.setattr(backups_route, "require_admin", lambda r, **kw: request.state.user)

        with pytest.raises(HTTPException) as exc_info:
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                backups_route.download_backup(request, "auth", "test.db.gz"),
            )
        assert exc_info.value.status_code == 403
        assert "re-authentication" in str(exc_info.value.detail)

    def test_get_non_auth_download_still_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GET requests for non-auth databases still serve the file."""
        from routes import backups as backups_route
        monkeypatch.setattr(backups_route, "BACKUP_ROOT", tmp_path)
        (tmp_path / "scarguard").mkdir()
        (tmp_path / "scarguard" / "test.db.gz").write_bytes(b"\x1f\x8b\x08")

        from unittest.mock import MagicMock

        request = MagicMock()
        request.state.user = {"user_id": 1, "username": "admin", "role": "admin", "is_admin": 1}
        monkeypatch.setattr(backups_route, "require_admin", lambda r, **kw: request.state.user)

        import asyncio
        resp = asyncio.get_event_loop().run_until_complete(
            backups_route.download_backup(request, "scarguard", "test.db.gz"),
        )
        # FileResponse indicates success — the file will be served.
        from fastapi.responses import FileResponse
        assert isinstance(resp, FileResponse)

    def test_post_auth_download_no_password_returns_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POST to auth download without password is rejected."""
        from routes import backups as backups_route
        monkeypatch.setattr(backups_route, "BACKUP_ROOT", tmp_path)
        (tmp_path / "auth").mkdir()
        (tmp_path / "auth" / "test.db.gz").write_bytes(b"x")

        from unittest.mock import MagicMock, AsyncMock
        from fastapi import HTTPException

        request = MagicMock()
        request.state.user = {"user_id": 1, "username": "admin", "role": "admin", "is_admin": 1}
        request.json = AsyncMock(return_value={})
        monkeypatch.setattr(backups_route, "require_admin", lambda r, **kw: request.state.user)

        with pytest.raises(HTTPException) as exc_info:
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                backups_route.download_backup_post(request, "auth", "test.db.gz"),
            )
        assert exc_info.value.status_code == 403
        assert "Password required" in str(exc_info.value.detail)

    def test_post_auth_download_wrong_password_returns_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POST with wrong password is rejected and audit-logged."""
        from routes import backups as backups_route
        monkeypatch.setattr(backups_route, "BACKUP_ROOT", tmp_path)
        (tmp_path / "auth").mkdir()
        (tmp_path / "auth" / "test.db.gz").write_bytes(b"x")

        import auth as auth_module
        from unittest.mock import MagicMock, AsyncMock, patch
        from fastapi import HTTPException

        request = MagicMock()
        request.state.user = {"user_id": 1, "username": "admin", "role": "admin", "is_admin": 1}
        request.client.host = "127.0.0.1"
        request.json = AsyncMock(return_value={"password": "wrong-pw"})
        monkeypatch.setattr(backups_route, "require_admin", lambda r, **kw: request.state.user)

        # Stub auth lookups: return a user with a known hash.
        fake_hash = auth_module.hash_password("correct-password")
        fake_user = {"id": 1, "username": "admin", "password_hash": fake_hash}

        with patch("auth.get_db") as mock_get_db, \
             patch("auth.get_user_by_id", return_value=fake_user), \
             patch("audit.record_request") as mock_audit:
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db

            with pytest.raises(HTTPException) as exc_info:
                import asyncio
                asyncio.get_event_loop().run_until_complete(
                    backups_route.download_backup_post(request, "auth", "test.db.gz"),
                )
            assert exc_info.value.status_code == 403
            assert "Password verification failed" in str(exc_info.value.detail)
            mock_audit.assert_called_once()
            assert mock_audit.call_args.kwargs["action"] == "backup.download.auth.failed"

    def test_post_auth_download_correct_password_serves_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POST with correct password serves the file and audit-logs success."""
        from routes import backups as backups_route
        monkeypatch.setattr(backups_route, "BACKUP_ROOT", tmp_path)
        (tmp_path / "auth").mkdir()
        (tmp_path / "auth" / "test.db.gz").write_bytes(b"\x1f\x8b\x08")

        import auth as auth_module
        from unittest.mock import MagicMock, AsyncMock, patch
        from fastapi.responses import FileResponse

        request = MagicMock()
        request.state.user = {"user_id": 1, "username": "admin", "role": "admin", "is_admin": 1}
        request.client.host = "127.0.0.1"
        request.json = AsyncMock(return_value={"password": "correct-password"})
        monkeypatch.setattr(backups_route, "require_admin", lambda r, **kw: request.state.user)

        fake_hash = auth_module.hash_password("correct-password")
        fake_user = {"id": 1, "username": "admin", "password_hash": fake_hash}

        with patch("auth.get_db") as mock_get_db, \
             patch("auth.get_user_by_id", return_value=fake_user), \
             patch("audit.record_request") as mock_audit:
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db

            import asyncio
            resp = asyncio.get_event_loop().run_until_complete(
                backups_route.download_backup_post(request, "auth", "test.db.gz"),
            )
            assert isinstance(resp, FileResponse)
            # Verify the success audit event was logged.
            assert mock_audit.call_count == 1
            assert mock_audit.call_args.kwargs["action"] == "backup.download.auth"

    def test_post_non_auth_download_skips_reauth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POST for non-auth databases does not require password."""
        from routes import backups as backups_route
        monkeypatch.setattr(backups_route, "BACKUP_ROOT", tmp_path)
        (tmp_path / "deterrent").mkdir()
        (tmp_path / "deterrent" / "test.db").write_bytes(b"data")

        from unittest.mock import MagicMock, AsyncMock
        from fastapi.responses import FileResponse

        request = MagicMock()
        request.state.user = {"user_id": 1, "username": "admin", "role": "admin", "is_admin": 1}
        request.json = AsyncMock(return_value={})
        monkeypatch.setattr(backups_route, "require_admin", lambda r, **kw: request.state.user)

        import asyncio
        resp = asyncio.get_event_loop().run_until_complete(
            backups_route.download_backup_post(request, "deterrent", "test.db"),
        )
        assert isinstance(resp, FileResponse)


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
