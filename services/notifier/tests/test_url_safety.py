"""Tests for shared/url_safety.py — SSRF defence for notifier webhook URLs."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from url_safety import UnsafeURLError, validate_external_url


class TestSchemeRejection:
    @pytest.mark.parametrize("url", [
        "ftp://example.com/foo",
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
        "data:text/plain,hello",
    ])
    def test_rejects_non_http(self, url: str) -> None:
        with pytest.raises(UnsafeURLError, match="scheme"):
            validate_external_url(url)

    @pytest.mark.parametrize("url", [
        "",
        "   ",
        "not-a-url",
    ])
    def test_rejects_empty_or_garbage(self, url: str) -> None:
        with pytest.raises(UnsafeURLError):
            validate_external_url(url)

    def test_rejects_no_hostname(self) -> None:
        with pytest.raises(UnsafeURLError, match="hostname"):
            validate_external_url("https:///")


class TestLiteralIPs:
    """Literal IPs should be checked without DNS round-trip."""

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/",
        "http://127.0.0.1:6379/",
        "http://[::1]/",
        "http://0.0.0.0/",
    ])
    def test_rejects_loopback(self, url: str) -> None:
        with pytest.raises(UnsafeURLError):
            validate_external_url(url)

    @pytest.mark.parametrize("url", [
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
    ])
    def test_rejects_rfc1918(self, url: str) -> None:
        with pytest.raises(UnsafeURLError):
            validate_external_url(url)

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",  # AWS/GCP IMDS
        "http://169.254.1.1/",  # link-local
    ])
    def test_rejects_link_local(self, url: str) -> None:
        with pytest.raises(UnsafeURLError):
            validate_external_url(url)

    def test_accepts_public_ip(self) -> None:
        # 1.1.1.1 (Cloudflare DNS) — not internal, public-routable.
        validate_external_url("https://1.1.1.1/")

    def test_allow_internal_permits_rfc1918(self) -> None:
        # Home Assistant on RFC1918 — opt-in via allow_internal=True.
        validate_external_url("http://192.168.1.50/api/", allow_internal=True)

    def test_allow_internal_still_rejects_loopback(self) -> None:
        # Loopback never makes sense — even with allow_internal=True.
        with pytest.raises(UnsafeURLError):
            validate_external_url("http://127.0.0.1/", allow_internal=True)

    def test_allow_internal_still_rejects_link_local(self) -> None:
        with pytest.raises(UnsafeURLError):
            validate_external_url("http://169.254.169.254/", allow_internal=True)

    def test_allow_internal_still_rejects_unspecified(self) -> None:
        # 0.0.0.0 is not a webhook target under any circumstance.
        with pytest.raises(UnsafeURLError):
            validate_external_url("http://0.0.0.0/", allow_internal=True)

    def test_allow_internal_still_rejects_multicast(self) -> None:
        with pytest.raises(UnsafeURLError):
            validate_external_url("http://224.0.0.1/", allow_internal=True)

    def test_allow_internal_still_rejects_reserved(self) -> None:
        with pytest.raises(UnsafeURLError):
            validate_external_url("http://240.0.0.1/", allow_internal=True)


class TestHostnameResolution:
    def test_rejects_hostname_resolving_to_loopback(self) -> None:
        # Mock getaddrinfo to claim a name resolves to loopback.
        with patch("url_safety.socket.getaddrinfo") as gai:
            import socket
            gai.return_value = [
                (socket.AF_INET, 0, 0, "", ("127.0.0.1", 0)),
            ]
            with pytest.raises(UnsafeURLError, match="internal"):
                validate_external_url("http://attacker-controlled.com/")

    def test_rejects_hostname_resolving_to_rfc1918(self) -> None:
        with patch("url_safety.socket.getaddrinfo") as gai:
            import socket
            gai.return_value = [
                (socket.AF_INET, 0, 0, "", ("10.0.0.5", 0)),
            ]
            with pytest.raises(UnsafeURLError):
                validate_external_url("http://internal.corp/")

    def test_accepts_hostname_resolving_to_public(self) -> None:
        # 1.1.1.1 is real public IP — not in any "reserved" range.
        with patch("url_safety.socket.getaddrinfo") as gai:
            import socket
            gai.return_value = [
                (socket.AF_INET, 0, 0, "", ("1.1.1.1", 0)),
            ]
            validate_external_url("https://example.com/")

    def test_dns_failure_raises(self) -> None:
        import socket
        with patch("url_safety.socket.getaddrinfo", side_effect=socket.gaierror("nope")):
            with pytest.raises(UnsafeURLError, match="DNS"):
                validate_external_url("https://nonexistent.invalid/")

    def test_one_bad_ip_blocks_whole_url(self) -> None:
        """Hostname resolving to BOTH a public IP AND a private IP must be rejected.
        Otherwise an attacker can DNS-rebind to a private target after the check."""
        with patch("url_safety.socket.getaddrinfo") as gai:
            import socket
            gai.return_value = [
                (socket.AF_INET, 0, 0, "", ("1.1.1.1", 0)),
                (socket.AF_INET, 0, 0, "", ("10.0.0.1", 0)),
            ]
            with pytest.raises(UnsafeURLError):
                validate_external_url("https://attacker.com/")


class TestDiscordWebhookHappy:
    def test_real_discord_url_validates(self) -> None:
        with patch("url_safety.socket.getaddrinfo") as gai:
            import socket
            gai.return_value = [
                (socket.AF_INET, 0, 0, "", ("162.159.135.232", 0)),
            ]
            validate_external_url(
                "https://discord.com/api/webhooks/123456789/abcdef",
            )
