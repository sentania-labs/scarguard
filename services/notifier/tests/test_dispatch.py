"""Unit tests for notifier dispatch logic."""

import json
from unittest.mock import MagicMock, patch

import pytest

from conftest import SAMPLE_EVENT


class TestDiscordNotifier:
    def _make(self, **overrides):
        from discord import DiscordNotifier

        cfg = {
            "webhook_url": "https://discord.com/api/webhooks/test/token",
            "mention_role": "",
            "include_snapshot": True,
            **overrides,
        }
        return DiscordNotifier(cfg)

    def test_sends_text_message_when_no_snapshot(self):
        notifier = self._make()
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
            notifier.send(SAMPLE_EVENT)
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert "Great Blue Heron" in payload["content"]
        assert "pond-north" in payload["content"]
        assert "87%" in payload["content"]

    def test_message_includes_mention_role(self):
        notifier = self._make(mention_role="123456789")
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
            notifier.send(SAMPLE_EVENT)
        content = mock_post.call_args[1]["json"]["content"]
        assert "<@&123456789>" in content

    def test_no_mention_when_role_empty(self):
        notifier = self._make(mention_role="")
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
            notifier.send(SAMPLE_EVENT)
        content = mock_post.call_args[1]["json"]["content"]
        assert "<@&" not in content

    def test_sends_multipart_when_snapshot_exists(self, tmp_path):
        snap = tmp_path / "frame.jpg"
        snap.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)  # minimal JPEG header

        event = {**SAMPLE_EVENT, "snapshot_path": str(snap)}
        notifier = self._make()

        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
            notifier.send(event)

        _, kwargs = mock_post.call_args
        # Multipart upload uses data= not json=
        assert "files" in kwargs
        assert "payload_json" in kwargs["data"]
        embed_payload = json.loads(kwargs["data"]["payload_json"])
        assert "embeds" in embed_payload
        assert "attachment://frame.jpg" in embed_payload["embeds"][0]["image"]["url"]

    def test_falls_back_to_text_when_snapshot_missing(self):
        event = {**SAMPLE_EVENT, "snapshot_path": "/nonexistent/frame.jpg"}
        notifier = self._make()

        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
            notifier.send(event)

        _, kwargs = mock_post.call_args
        # Missing file → text-only path (json=, no files=)
        assert "json" in kwargs
        assert "files" not in kwargs

    def test_does_not_raise_on_request_error(self):
        import requests as req_lib

        notifier = self._make()
        with patch("requests.post", side_effect=req_lib.ConnectionError("timeout")):
            # Should log and return, never raise
            notifier.send(SAMPLE_EVENT)

    def test_include_snapshot_false_skips_file(self, tmp_path):
        snap = tmp_path / "frame.jpg"
        snap.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)
        event = {**SAMPLE_EVENT, "snapshot_path": str(snap)}
        notifier = self._make(include_snapshot=False)

        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
            notifier.send(event)

        _, kwargs = mock_post.call_args
        assert "files" not in kwargs


class TestEmailNotifier:
    def _make(self, **overrides):
        from email_notifier import EmailNotifier

        cfg = {
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "user@example.com",
            "smtp_pass": "secret",
            "to_addresses": ["alert@example.com"],
            "include_snapshot": False,
            **overrides,
        }
        return EmailNotifier(cfg)

    def test_skips_send_when_no_to_addresses(self):
        notifier = self._make(to_addresses=[])
        with patch("smtplib.SMTP") as mock_smtp:
            notifier.send(SAMPLE_EVENT)
        mock_smtp.assert_not_called()

    def test_subject_contains_class_name(self):
        notifier = self._make()
        with patch.object(notifier, "_send_message") as mock_send:
            notifier.send(SAMPLE_EVENT)
        msg = mock_send.call_args[0][0]
        assert "Great Blue Heron" in msg["Subject"]

    def test_body_contains_camera_and_confidence(self):
        notifier = self._make()
        captured = []
        with patch.object(notifier, "_send_message", side_effect=lambda m: captured.append(m)):
            notifier.send(SAMPLE_EVENT)
        body = captured[0].get_payload()[0].get_payload()
        assert "pond-north" in body
        assert "87%" in body

    def test_attaches_snapshot_when_include_true(self, tmp_path):
        snap = tmp_path / "frame.jpg"
        snap.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)
        event = {**SAMPLE_EVENT, "snapshot_path": str(snap)}
        notifier = self._make(include_snapshot=True)

        sent_msgs = []
        with patch.object(notifier, "_send_message", side_effect=sent_msgs.append):
            notifier.send(event)

        parts = sent_msgs[0].get_payload()
        # Multipart: [MIMEText body, MIMEBase attachment]
        assert len(parts) == 2
        attachment = parts[1]
        assert attachment.get_filename() == "frame.jpg"

    def test_does_not_raise_on_smtp_error(self):
        import smtplib

        notifier = self._make()
        with patch.object(notifier, "_send_message", side_effect=smtplib.SMTPException("fail")):
            # Should log and return, never propagate
            notifier.send(SAMPLE_EVENT)


class TestDispatchRouting:
    def test_dispatch_calls_all_notifiers(self):
        from main import dispatch

        n1, n2 = MagicMock(), MagicMock()
        dispatch(SAMPLE_EVENT, [n1, n2])
        n1.send.assert_called_once_with(SAMPLE_EVENT)
        n2.send.assert_called_once_with(SAMPLE_EVENT)

    def test_dispatch_continues_after_notifier_exception(self):
        from main import dispatch

        failing = MagicMock(side_effect=RuntimeError("boom"))
        ok = MagicMock()
        dispatch(SAMPLE_EVENT, [failing, ok])
        ok.send.assert_called_once_with(SAMPLE_EVENT)

    def test_build_notifiers_discord_enabled(self):
        from main import build_notifiers

        cfg = {
            "discord": {
                "enabled": True,
                "webhook_url": "https://discord.com/api/webhooks/x/y",
            }
        }
        notifiers = build_notifiers(cfg)
        assert len(notifiers) == 1
        from discord import DiscordNotifier

        assert isinstance(notifiers[0], DiscordNotifier)

    def test_build_notifiers_discord_disabled(self):
        from main import build_notifiers

        cfg = {"discord": {"enabled": False, "webhook_url": "https://discord.com/..."}}
        notifiers = build_notifiers(cfg)
        assert notifiers == []

    def test_build_notifiers_no_webhook_url(self):
        from main import build_notifiers

        cfg = {"discord": {"enabled": True, "webhook_url": ""}}
        notifiers = build_notifiers(cfg)
        assert notifiers == []
