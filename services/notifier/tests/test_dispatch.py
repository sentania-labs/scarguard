"""Unit tests for notifier dispatch logic."""

import json
from unittest.mock import MagicMock, patch

import pytest
from conftest import SAMPLE_EVENT, write_image

DISCORD_HOST = "discord.example"
DISCORD_IP = "162.159.135.232"


@pytest.fixture()
def discord_server(net, http_server):
    """discord.example resolves to a public IP routed to the local fixture server."""
    net.dns[DISCORD_HOST] = [DISCORD_IP]
    net.routes[(DISCORD_IP, 80)] = ("127.0.0.1", http_server.port)
    return http_server


class TestDiscordNotifier:
    def _make(self, **overrides):
        from discord import DiscordNotifier

        cfg = {
            "webhook_url": f"http://{DISCORD_HOST}/api/webhooks/test/token",
            "mention_role": "",
            "include_snapshot": True,
            **overrides,
        }
        return DiscordNotifier(cfg)

    def test_sends_text_message_when_no_snapshot(self, discord_server):
        notifier = self._make()
        notifier.send(SAMPLE_EVENT)
        assert len(discord_server.requests) == 1
        req = discord_server.requests[0]
        assert req.method == "POST"
        assert req.path == "/api/webhooks/test/token"
        assert req.headers["Host"] == DISCORD_HOST
        payload = json.loads(req.body)
        assert "Great Blue Heron" in payload["content"]
        assert "pond-north" in payload["content"]
        assert "87%" in payload["content"]

    def test_message_includes_mention_role(self, discord_server):
        notifier = self._make(mention_role="123456789")
        notifier.send(SAMPLE_EVENT)
        content = json.loads(discord_server.requests[0].body)["content"]
        assert "<@&123456789>" in content

    def test_no_mention_when_role_empty(self, discord_server):
        notifier = self._make(mention_role="")
        notifier.send(SAMPLE_EVENT)
        content = json.loads(discord_server.requests[0].body)["content"]
        assert "<@&" not in content

    def test_sends_multipart_when_snapshot_exists(self, discord_server, snapshot_dir):
        snap = write_image(snapshot_dir / "frame.jpg")
        event = {**SAMPLE_EVENT, "snapshot_path": str(snap)}
        notifier = self._make()
        notifier.send(event)

        req = discord_server.requests[0]
        # Multipart upload carries payload_json plus the image file
        assert req.headers["Content-Type"].startswith("multipart/form-data")
        assert b'name="payload_json"' in req.body
        assert b'filename="frame.jpg"' in req.body
        assert snap.read_bytes() in req.body
        assert b"attachment://frame.jpg" in req.body

    def test_falls_back_to_text_when_snapshot_missing(self, discord_server, snapshot_dir):
        event = {**SAMPLE_EVENT, "snapshot_path": str(snapshot_dir / "missing.jpg")}
        notifier = self._make()
        notifier.send(event)
        req = discord_server.requests[0]
        # Missing file → text-only path (JSON body, no multipart)
        assert req.headers["Content-Type"] == "application/json"
        assert "embeds" not in json.loads(req.body)

    def test_raises_on_request_error(self, net):
        import requests as req_lib

        net.dns[DISCORD_HOST] = [DISCORD_IP]  # no route → connection refused
        notifier = self._make()
        # Propagates so the dispatch layer can enqueue for retry
        with pytest.raises(req_lib.ConnectionError):
            notifier.send(SAMPLE_EVENT)

    def test_include_snapshot_false_skips_file(self, discord_server, snapshot_dir):
        snap = write_image(snapshot_dir / "frame.jpg")
        event = {**SAMPLE_EVENT, "snapshot_path": str(snap)}
        notifier = self._make(include_snapshot=False)
        notifier.send(event)
        assert discord_server.requests[0].headers["Content-Type"] == "application/json"


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
        # Structure: related > [alternative > [plaintext, html], ...]
        alt_part = captured[0].get_payload()[0]
        plain_body = alt_part.get_payload()[0].get_payload()
        assert "pond-north" in plain_body
        assert "87%" in plain_body

    def test_attaches_snapshot_when_include_true(self, snapshot_dir):
        snap = write_image(snapshot_dir / "frame.jpg")
        event = {**SAMPLE_EVENT, "snapshot_path": str(snap)}
        notifier = self._make(include_snapshot=True)

        sent_msgs = []
        with patch.object(notifier, "_send_message", side_effect=sent_msgs.append):
            notifier.send(event)

        parts = sent_msgs[0].get_payload()
        # Structure: related > [alternative, inline image]
        assert len(parts) == 2
        inline_img = parts[1]
        assert inline_img["Content-ID"] == "<snapshot>"
        assert inline_img.get_filename() == "snapshot.jpg"

    def test_raises_on_smtp_error(self):
        import smtplib

        notifier = self._make()
        with patch.object(notifier, "_send_message", side_effect=smtplib.SMTPException("fail")):
            # Propagates so the dispatch layer can enqueue for retry
            with pytest.raises(smtplib.SMTPException):
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

    def test_build_notifiers_discord_channel(self):
        from main import build_notifiers

        cfg = {
            "channels": [
                {
                    "name": "alerts",
                    "type": "discord",
                    "webhook_url": "https://discord.com/api/webhooks/x/y",
                }
            ]
        }
        notifiers = build_notifiers(cfg)
        assert len(notifiers) == 1
        from discord import DiscordNotifier

        assert isinstance(notifiers[0], DiscordNotifier)

    def test_build_notifiers_discord_channel_disabled(self):
        from main import build_notifiers

        cfg = {
            "channels": [
                {
                    "name": "alerts",
                    "type": "discord",
                    "enabled": False,
                    "webhook_url": "https://discord.com/api/webhooks/x/y",
                }
            ]
        }
        notifiers = build_notifiers(cfg)
        assert notifiers == []

    def test_build_notifiers_discord_channel_no_webhook_url(self):
        from main import build_notifiers

        cfg = {
            "channels": [
                {"name": "alerts", "type": "discord", "webhook_url": ""}
            ]
        }
        notifiers = build_notifiers(cfg)
        assert notifiers == []


NTFY_HOST = "ntfy.example"
NTFY_IP = "159.203.148.75"


@pytest.fixture()
def ntfy_server(net, http_server):
    net.dns[NTFY_HOST] = [NTFY_IP]
    net.routes[(NTFY_IP, 80)] = ("127.0.0.1", http_server.port)
    return http_server


class TestNtfyNotifier:
    def _make(self, **overrides):
        from ntfy import NtfyNotifier

        cfg = {
            "topic": "scarguard-test",
            "server": f"http://{NTFY_HOST}",
            "include_snapshot": True,
            **overrides,
        }
        return NtfyNotifier(cfg)

    def test_sends_text_message(self, ntfy_server):
        notifier = self._make()
        notifier.send(SAMPLE_EVENT)
        assert len(ntfy_server.requests) == 1
        req = ntfy_server.requests[0]
        assert (req.method, req.path) == ("POST", "/scarguard-test")
        assert b"pond-north" in req.body
        assert b"87%" in req.body

    def test_sends_with_snapshot(self, ntfy_server, snapshot_dir):
        snap = write_image(snapshot_dir / "frame.jpg")
        event = {**SAMPLE_EVENT, "snapshot_path": str(snap)}
        notifier = self._make()
        notifier.send(event)
        req = ntfy_server.requests[0]
        assert req.method == "PUT"
        assert req.headers["Filename"] == "frame.jpg"
        assert req.body == snap.read_bytes()

    def test_include_snapshot_false_skips_file(self, ntfy_server, snapshot_dir):
        snap = write_image(snapshot_dir / "frame.jpg")
        event = {**SAMPLE_EVENT, "snapshot_path": str(snap)}
        notifier = self._make(include_snapshot=False)
        notifier.send(event)
        # Should use POST (text), not PUT (file)
        assert [r.method for r in ntfy_server.requests] == ["POST"]

    def test_auth_token_in_headers(self, ntfy_server):
        notifier = self._make(token="tk_mytoken")
        notifier.send(SAMPLE_EVENT)
        assert ntfy_server.requests[0].headers["Authorization"] == "Bearer tk_mytoken"

    def test_basic_auth_in_headers(self, ntfy_server):
        import base64
        notifier = self._make(username="user", password="pass")
        notifier.send(SAMPLE_EVENT)
        expected = f"Basic {base64.b64encode(b'user:pass').decode()}"
        assert ntfy_server.requests[0].headers["Authorization"] == expected

    def test_priority_clamped(self):
        notifier = self._make(priority=10)
        assert notifier._priority == 5
        notifier2 = self._make(priority=0)
        assert notifier2._priority == 1

    def test_raises_on_request_error(self, net):
        import requests as req_lib
        net.dns[NTFY_HOST] = [NTFY_IP]  # no route → connection refused
        notifier = self._make()
        with pytest.raises(req_lib.ConnectionError):
            notifier.send(SAMPLE_EVENT)

    def test_build_notifiers_ntfy_channel(self):
        from main import build_notifiers

        cfg = {
            "channels": [
                {
                    "name": "phone",
                    "type": "ntfy",
                    "enabled": True,
                    "topic": "scarguard-test",
                    "server": "https://ntfy.sh",
                }
            ]
        }
        notifiers = build_notifiers(cfg)
        assert len(notifiers) == 1
        from ntfy import NtfyNotifier
        assert isinstance(notifiers[0], NtfyNotifier)
        assert notifiers[0].name == "phone"

    def test_build_notifiers_ntfy_no_topic(self):
        from main import build_notifiers

        cfg = {
            "channels": [
                {"name": "phone", "type": "ntfy", "enabled": True, "topic": ""}
            ]
        }
        notifiers = build_notifiers(cfg)
        assert len(notifiers) == 0


class TestDispatchFiltering:
    def test_dispatch_suppresses_when_actions_triggered_none(self):
        """actions_triggered=None means action rules exist but no match - send nothing."""
        from main import dispatch

        event = {**SAMPLE_EVENT, "actions_triggered": None}
        n1, n2 = MagicMock(), MagicMock()
        dispatch(event, [n1, n2])
        n1.send.assert_not_called()
        n2.send.assert_not_called()

    def test_dispatch_notifies_all_when_actions_triggered_empty(self):
        """actions_triggered=[] means no rules configured - notify all channels."""
        from main import dispatch

        event = {**SAMPLE_EVENT, "actions_triggered": []}
        n1, n2 = MagicMock(), MagicMock()
        dispatch(event, [n1, n2])
        n1.send.assert_called_once()
        n2.send.assert_called_once()

    def test_dispatch_filters_to_named_channels(self):
        """actions_triggered with channel names filters to matching notifiers only."""
        from main import dispatch

        event = {**SAMPLE_EVENT, "actions_triggered": ["bird-alerts-email"]}
        email = MagicMock()
        email.name = "bird-alerts-email"
        discord = MagicMock()
        discord.name = "bird-alerts-discord"
        dispatch(event, [email, discord])
        email.send.assert_called_once()
        discord.send.assert_not_called()
