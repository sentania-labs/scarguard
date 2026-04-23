"""Tests for shared/secret_box.py — at-rest envelope encryption."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
import secret_box


@pytest.fixture()
def key() -> bytes:
    """A deterministic 32-byte key suitable for the secret_box helpers."""
    return base64.b64encode(b"\x00" * 32)


class TestRoundTrip:
    def test_basic_round_trip(self, key: bytes) -> None:
        ciphertext = secret_box.encrypt("super-secret", key)
        assert ciphertext.startswith(secret_box.PREFIX)
        assert secret_box.decrypt(ciphertext, key) == "super-secret"

    def test_encrypt_idempotent(self, key: bytes) -> None:
        once = secret_box.encrypt("hi", key)
        twice = secret_box.encrypt(once, key)
        assert once == twice

    def test_decrypt_passthrough_on_plaintext(self, key: bytes) -> None:
        assert secret_box.decrypt("not encrypted", key) == "not encrypted"

    def test_encrypt_passthrough_on_empty(self, key: bytes) -> None:
        assert secret_box.encrypt("", key) == ""
        assert secret_box.encrypt(None, key) is None  # type: ignore[arg-type]

    def test_decrypt_with_wrong_key_raises(self, key: bytes) -> None:
        ciphertext = secret_box.encrypt("hi", key)
        wrong = base64.b64encode(b"\x01" * 32)
        with pytest.raises(secret_box.SecretKeyMissing):
            secret_box.decrypt(ciphertext, wrong)

    def test_is_encrypted(self, key: bytes) -> None:
        assert secret_box.is_encrypted(secret_box.encrypt("hi", key))
        assert not secret_box.is_encrypted("hi")
        assert not secret_box.is_encrypted("")
        assert not secret_box.is_encrypted(None)


class TestKeyNormalisation:
    def test_accepts_raw_32_bytes(self) -> None:
        raw = b"\x07" * 32
        # Use raw key directly with internal helper.
        assert secret_box._normalize_key(raw) == raw

    def test_accepts_b64_string(self) -> None:
        raw = b"\x05" * 32
        encoded = base64.b64encode(raw)
        assert secret_box._normalize_key(encoded) == raw

    def test_accepts_url_safe_b64(self) -> None:
        raw = b"\x09" * 32
        encoded = base64.urlsafe_b64encode(raw)
        assert secret_box._normalize_key(encoded) == raw

    def test_rejects_short_key(self) -> None:
        with pytest.raises(secret_box.SecretKeyMissing):
            secret_box._normalize_key(b"too short")

    def test_rejects_garbage(self) -> None:
        with pytest.raises(secret_box.SecretKeyMissing):
            secret_box._normalize_key(b"!@#$%^&*()" * 5)


class TestKeyFile:
    def test_write_then_load(self, tmp_path: Path) -> None:
        key_path = tmp_path / "k"
        assert secret_box.write_key_if_missing(str(key_path)) is True
        # File should exist with mode 0o600.
        assert key_path.stat().st_mode & 0o777 == 0o600
        loaded = secret_box.load_key(str(key_path))
        assert len(loaded) >= 32

    def test_write_idempotent(self, tmp_path: Path) -> None:
        key_path = tmp_path / "k"
        assert secret_box.write_key_if_missing(str(key_path)) is True
        assert secret_box.write_key_if_missing(str(key_path)) is False

    def test_load_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(secret_box.SecretKeyMissing):
            secret_box.load_key(str(tmp_path / "missing"))

    def test_try_load_returns_none_when_missing(self, tmp_path: Path) -> None:
        assert secret_box.try_load_key(str(tmp_path / "missing")) is None

    def test_load_empty_file_raises(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.write_text("")
        with pytest.raises(secret_box.SecretKeyMissing):
            secret_box.load_key(str(empty))


class TestEncryptInPlace:
    def test_encrypts_tuya_credentials(self, key: bytes) -> None:
        cfg = {
            "deterrent": {
                "tuya": {
                    "api_key": "tuya-public-id",
                    "api_secret": "tuya-secret-secret",
                },
            },
        }
        n = secret_box.encrypt_in_place(cfg, key)
        assert n == 2
        assert cfg["deterrent"]["tuya"]["api_key"].startswith(secret_box.PREFIX)
        assert cfg["deterrent"]["tuya"]["api_secret"].startswith(secret_box.PREFIX)

    def test_encrypts_channel_secrets(self, key: bytes) -> None:
        cfg = {
            "notifications": {
                "channels": [
                    {
                        "name": "discord-1",
                        "type": "discord",
                        "webhook_url": "https://discord.com/api/webhooks/...",
                    },
                    {
                        "name": "smtp-1",
                        "type": "email",
                        "smtp_pass": "letmein",
                    },
                    {
                        "name": "ntfy-1",
                        "type": "ntfy",
                        "token": "tk_xyz",
                        "password": "basicpw",
                    },
                ],
            },
        }
        n = secret_box.encrypt_in_place(cfg, key)
        assert n == 4
        ch = cfg["notifications"]["channels"]
        assert ch[0]["webhook_url"].startswith(secret_box.PREFIX)
        assert ch[1]["smtp_pass"].startswith(secret_box.PREFIX)
        assert ch[2]["token"].startswith(secret_box.PREFIX)
        assert ch[2]["password"].startswith(secret_box.PREFIX)

    def test_idempotent(self, key: bytes) -> None:
        cfg = {"deterrent": {"tuya": {"api_key": "k", "api_secret": "s"}}}
        first = secret_box.encrypt_in_place(cfg, key)
        second = secret_box.encrypt_in_place(cfg, key)
        assert first == 2
        assert second == 0  # already encrypted

    def test_skips_empty_values(self, key: bytes) -> None:
        cfg = {"deterrent": {"tuya": {"api_key": "", "api_secret": None}}}
        n = secret_box.encrypt_in_place(cfg, key)
        assert n == 0

    def test_skips_missing_sections(self, key: bytes) -> None:
        cfg: dict = {}
        assert secret_box.encrypt_in_place(cfg, key) == 0

    def test_round_trip_in_place(self, key: bytes) -> None:
        cfg = {
            "deterrent": {"tuya": {"api_key": "k", "api_secret": "s"}},
            "notifications": {"channels": [
                {"name": "d", "type": "discord", "webhook_url": "https://x.com/h"},
            ]},
        }
        secret_box.encrypt_in_place(cfg, key)
        secret_box.decrypt_in_place(cfg, key)
        assert cfg["deterrent"]["tuya"]["api_key"] == "k"
        assert cfg["deterrent"]["tuya"]["api_secret"] == "s"
        assert cfg["notifications"]["channels"][0]["webhook_url"] == "https://x.com/h"


class TestPlaintextDetection:
    def test_has_plaintext_secrets_returns_true(self, key: bytes) -> None:
        cfg = {"deterrent": {"tuya": {"api_key": "plaintext", "api_secret": "x"}}}
        assert secret_box.has_plaintext_secrets(cfg) is True

    def test_returns_false_when_all_encrypted(self, key: bytes) -> None:
        cfg = {"deterrent": {"tuya": {"api_key": "k", "api_secret": "s"}}}
        secret_box.encrypt_in_place(cfg, key)
        assert secret_box.has_plaintext_secrets(cfg) is False

    def test_returns_false_when_no_secrets_present(self) -> None:
        assert secret_box.has_plaintext_secrets({}) is False
        assert secret_box.has_plaintext_secrets({"system": {"armed": True}}) is False

    def test_detects_channel_plaintext(self) -> None:
        cfg = {"notifications": {"channels": [{"smtp_pass": "letmein"}]}}
        assert secret_box.has_plaintext_secrets(cfg) is True


class TestConfigStoreIntegration:
    """Sanity: when config_store loads a YAML with encrypted fields and a
    valid key on disk, the returned dict has plaintext values."""

    def test_load_decrypts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: bytes) -> None:
        import config_store

        cfg_path = tmp_path / "scarguard.yml"
        key_path = tmp_path / "secret_key"
        key_path.write_bytes(key)

        monkeypatch.setattr(config_store, "CONFIG_PATH", cfg_path)
        monkeypatch.setattr(secret_box, "DEFAULT_KEY_PATH", str(key_path))

        # Write an encrypted YAML by hand
        encrypted = secret_box.encrypt("very-secret-password", key)
        cfg_path.write_text(
            "deterrent:\n"
            "  tuya:\n"
            "    api_key: pub-id\n"
            f"    api_secret: {encrypted}\n",
        )
        loaded = config_store.load()
        assert loaded["deterrent"]["tuya"]["api_secret"] == "very-secret-password"

    def test_save_encrypts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: bytes) -> None:
        import config_store

        cfg_path = tmp_path / "scarguard.yml"
        key_path = tmp_path / "secret_key"
        key_path.write_bytes(key)

        monkeypatch.setattr(config_store, "CONFIG_PATH", cfg_path)
        monkeypatch.setattr(secret_box, "DEFAULT_KEY_PATH", str(key_path))

        config_store.save({
            "deterrent": {"tuya": {"api_key": "pub", "api_secret": "plaintext-secret"}},
        })
        on_disk = cfg_path.read_text()
        assert "plaintext-secret" not in on_disk
        assert secret_box.PREFIX in on_disk

    def test_save_then_load_preserves_secret(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: bytes) -> None:
        import config_store

        cfg_path = tmp_path / "scarguard.yml"
        key_path = tmp_path / "secret_key"
        key_path.write_bytes(key)

        monkeypatch.setattr(config_store, "CONFIG_PATH", cfg_path)
        monkeypatch.setattr(secret_box, "DEFAULT_KEY_PATH", str(key_path))

        config_store.save({
            "deterrent": {"tuya": {"api_key": "pub-key", "api_secret": "round-trip-secret"}},
        })
        loaded = config_store.load()
        assert loaded["deterrent"]["tuya"]["api_secret"] == "round-trip-secret"
        assert loaded["deterrent"]["tuya"]["api_key"] == "pub-key"

    def test_no_key_means_save_writes_plaintext(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Migration mode: no key on disk → save leaves plaintext."""
        import config_store

        cfg_path = tmp_path / "scarguard.yml"
        monkeypatch.setattr(config_store, "CONFIG_PATH", cfg_path)
        monkeypatch.setattr(secret_box, "DEFAULT_KEY_PATH", str(tmp_path / "missing-key"))

        config_store.save({
            "deterrent": {"tuya": {"api_key": "k", "api_secret": "still-plain"}},
        })
        assert "still-plain" in cfg_path.read_text()
