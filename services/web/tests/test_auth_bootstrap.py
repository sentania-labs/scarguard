"""Tests for v1.14 auth additions: bootstrap-token helpers and password policy."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


class TestBootstrapTokenHelpers:
    def test_read_returns_none_when_missing(self, tmp_path: Path, monkeypatch) -> None:
        from routes import auth as auth_routes
        monkeypatch.setattr(auth_routes, "BOOTSTRAP_TOKEN_PATH", str(tmp_path / "no-such-file"))
        monkeypatch.delenv("SCARGUARD_BOOTSTRAP_TOKEN", raising=False)
        assert auth_routes._read_bootstrap_token() is None

    def test_read_returns_file_contents(self, tmp_path: Path, monkeypatch) -> None:
        token_file = tmp_path / "btok"
        token_file.write_text("disk-token\n")
        from routes import auth as auth_routes
        monkeypatch.setattr(auth_routes, "BOOTSTRAP_TOKEN_PATH", str(token_file))
        monkeypatch.delenv("SCARGUARD_BOOTSTRAP_TOKEN", raising=False)
        assert auth_routes._read_bootstrap_token() == "disk-token"

    def test_env_overrides_file(self, tmp_path: Path, monkeypatch) -> None:
        token_file = tmp_path / "btok"
        token_file.write_text("disk-token")
        from routes import auth as auth_routes
        monkeypatch.setattr(auth_routes, "BOOTSTRAP_TOKEN_PATH", str(token_file))
        monkeypatch.setenv("SCARGUARD_BOOTSTRAP_TOKEN", "env-token")
        assert auth_routes._read_bootstrap_token() == "env-token"

    def test_consume_deletes_file(self, tmp_path: Path, monkeypatch) -> None:
        token_file = tmp_path / "btok"
        token_file.write_text("x")
        from routes import auth as auth_routes
        monkeypatch.setattr(auth_routes, "BOOTSTRAP_TOKEN_PATH", str(token_file))
        auth_routes._consume_bootstrap_token()
        assert not token_file.exists()

    def test_consume_idempotent(self, tmp_path: Path, monkeypatch) -> None:
        from routes import auth as auth_routes
        monkeypatch.setattr(auth_routes, "BOOTSTRAP_TOKEN_PATH", str(tmp_path / "missing"))
        # Calling on a missing file must not raise.
        auth_routes._consume_bootstrap_token()
        auth_routes._consume_bootstrap_token()


class TestPasswordPolicy:
    def test_min_length_is_12(self) -> None:
        from routes.auth import MIN_PASSWORD_LEN
        assert MIN_PASSWORD_LEN == 12

    @pytest.mark.parametrize("pw", [
        "password",
        "password123",
        "admin",
        "letmein",
        "scarguard",
        "scarguard123",
        "12345678",
        "123456789",
        "qwerty",
    ])
    def test_known_common_passwords_rejected(self, pw: str) -> None:
        from routes.auth import _is_common_password
        assert _is_common_password(pw) is True

    @pytest.mark.parametrize("pw", [
        "PASSWORD",        # case-insensitive
        "Password",
        "  password  ",    # whitespace stripped
    ])
    def test_common_check_is_case_insensitive(self, pw: str) -> None:
        from routes.auth import _is_common_password
        assert _is_common_password(pw) is True

    @pytest.mark.parametrize("pw", [
        "correct horse battery staple",
        "MyKoiPondGuardian!",
        "Tr0ub4dor&3-but-longer",
        "scarwillsurvive2026",
    ])
    def test_strong_passwords_accepted(self, pw: str) -> None:
        from routes.auth import _is_common_password
        assert _is_common_password(pw) is False
