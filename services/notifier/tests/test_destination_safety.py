"""FDY-0571 (SG-19, SG-20, SG-28): notifications reach only validated
destinations, over verified TLS, with only genuine snapshot images attached.

Every test drives the production notifier classes against local fixture
servers (see conftest). FakeNet answers DNS and delivers dials for the
validated address to the fixture; ``net.dials`` shows exactly which address
the notifier connected to, and an empty fixture log proves nothing (no
credentials, no body) was sent.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from pathlib import Path

import pytest
import requests
from conftest import SAMPLE_EVENT, HTTPFixture, SMTPFixture, make_certs, write_image
from url_safety import UnsafeURLError

LAN_IP = "192.168.77.10"
PUBLIC_IP = "93.184.216.34"

DIGEST_REPORT = {
    "_digest": True,
    "period_label": "Daily",
    "generated_at": "2026-03-20T06:00:00",
    "detections": {"total": 3, "change_pct": 0, "by_class": {"great_blue_heron": 3}},
    "visits": {"total": 1, "top": []},
    "performance": {"status": "green", "avg_cpu_pct": None, "avg_gpu_pct": None,
                    "avg_gpu_temp": None, "camera_offline_total": 0},
    "storage": {"total_mb": 1, "snapshots_mb": 1, "database_mb": 0, "models_mb": 0},
    "training": {"protected_events": 0, "pruneable_events": 0, "fp_rate_pct": None},
}

ALWAYS_DENIED = [
    "127.0.0.1",          # loopback
    "::1",                # loopback v6
    "::ffff:127.0.0.1",   # loopback via v4-mapped v6
    "169.254.169.254",    # cloud metadata (link-local)
    "fd00:ec2::254",      # EC2 metadata v6
    "100.100.100.200",    # Alibaba metadata (inside 100.64/10)
    "172.17.0.1",         # default Docker bridge
    "172.24.0.5",         # ScarGuard compose network (redis, web, ...)
    "0.0.0.0",
]


def _webhook(**cfg):
    from webhook import WebhookNotifier

    return WebhookNotifier({"name": "hook", "method": "POST", **cfg})


# ── Webhook: LAN opt-in, always-denied ranges, redirects, rebinding ─────────


class TestWebhookDestinations:
    def test_lan_webhook_with_allow_internal_delivers_token(self, net, http_server):
        net.dns["ha.lan"] = [LAN_IP]
        net.routes[(LAN_IP, 8123)] = ("127.0.0.1", http_server.port)
        hook = _webhook(url="http://ha.lan:8123/api/webhook/pond", allow_internal=True,
                        auth_token="secret-token")
        hook.send(SAMPLE_EVENT)

        assert net.dials == [(LAN_IP, 8123)]
        req = http_server.requests[0]
        assert req.path == "/api/webhook/pond"
        assert req.headers["Authorization"] == "Bearer secret-token"
        assert req.headers["Host"] == "ha.lan:8123"

    def test_lan_webhook_without_allow_internal_sends_nothing(self, net, http_server):
        net.dns["ha.lan"] = [LAN_IP]
        net.routes[(LAN_IP, 8123)] = ("127.0.0.1", http_server.port)
        hook = _webhook(url="http://ha.lan:8123/api/webhook/pond", auth_token="secret-token")
        with pytest.raises(UnsafeURLError, match="allow_internal"):
            hook.send(SAMPLE_EVENT)
        assert net.dials == []
        assert http_server.requests == []

    def test_literal_lan_ip_without_allow_internal_disables_channel(self, net, http_server):
        hook = _webhook(url=f"http://{LAN_IP}/hook", auth_token="secret-token")
        hook.send(SAMPLE_EVENT)  # suppressed, not raised
        assert net.dials == [] and http_server.requests == []

    @pytest.mark.parametrize("addr", ALWAYS_DENIED)
    def test_hostname_resolving_to_denied_range_refused_even_with_allow_internal(
        self, net, http_server, addr,
    ):
        net.dns["innocent.example"] = [addr]
        net.routes[(addr, 80)] = ("127.0.0.1", http_server.port)
        hook = _webhook(url="http://innocent.example/hook", allow_internal=True,
                        auth_token="secret-token")
        with pytest.raises(UnsafeURLError):
            hook.send(SAMPLE_EVENT)
        assert net.dials == []
        assert http_server.requests == []

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:6379/",
        "http://172.17.0.1/",
        "http://172.24.0.2:8000/",
        "http://redis:6379/",
        "http://localhost/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://2130706433/",  # 127.0.0.1 in decimal
        "http://[::ffff:169.254.169.254]/",
        "file:///config/scarguard.yml",
    ])
    def test_denied_literal_urls_disable_channel_even_with_allow_internal(self, net, url):
        hook = _webhook(url=url, allow_internal=True, auth_token="secret-token")
        hook.send(SAMPLE_EVENT)
        assert net.dials == []
        assert net.lookups == []

    def test_mixed_public_and_private_answer_refused(self, net, http_server):
        net.dns["split.example"] = [PUBLIC_IP, "10.0.0.7"]
        net.routes[(PUBLIC_IP, 80)] = ("127.0.0.1", http_server.port)
        with pytest.raises(UnsafeURLError, match="10.0.0.7"):
            _webhook(url="http://split.example/hook").send(SAMPLE_EVENT)
        assert net.dials == [] and http_server.requests == []

    def test_redirect_to_metadata_is_not_followed(self, net, http_server):
        from safe_http import RedirectRefusedError

        net.dns["hooks.example"] = [PUBLIC_IP]
        net.routes[(PUBLIC_IP, 80)] = ("127.0.0.1", http_server.port)
        http_server.responses.append(
            (307, {"Location": "http://169.254.169.254/latest/meta-data/iam"}),
        )
        hook = _webhook(url="http://hooks.example/hook", auth_token="secret-token")
        with pytest.raises(RedirectRefusedError):
            hook.send(SAMPLE_EVENT)
        assert len(http_server.requests) == 1
        assert net.dials == [(PUBLIC_IP, 80)]

    def test_dns_rebinding_cannot_redirect_the_connection(self, net, http_server):
        answers = iter([[PUBLIC_IP], ["127.0.0.1"], ["127.0.0.1"]])
        net.dns["rebind.example"] = lambda: next(answers)
        net.routes[(PUBLIC_IP, 80)] = ("127.0.0.1", http_server.port)
        hook = _webhook(url="http://rebind.example/hook", auth_token="secret-token")

        hook.send(SAMPLE_EVENT)
        # One lookup per send, and the connection went to the checked address.
        assert net.lookups == ["rebind.example"]
        assert net.dials == [(PUBLIC_IP, 80)]

        # The rebound answer is checked on the next send and refused.
        with pytest.raises(UnsafeURLError, match="loopback"):
            hook.send(SAMPLE_EVENT)
        assert net.dials == [(PUBLIC_IP, 80)]
        assert len(http_server.requests) == 1

    def test_proxy_environment_is_ignored(self, net, http_server, monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", "http://169.254.169.254:3128")
        monkeypatch.setenv("http_proxy", "http://169.254.169.254:3128")
        net.dns["hooks.example"] = [PUBLIC_IP]
        net.routes[(PUBLIC_IP, 80)] = ("127.0.0.1", http_server.port)
        _webhook(url="http://hooks.example/hook").send(SAMPLE_EVENT)
        assert net.dials == [(PUBLIC_IP, 80)]

    def test_timeout_on_one_address_falls_through_to_next(self, net, http_server):
        net.dns["dual.example"] = ["2606:4700::1111", PUBLIC_IP]
        net.blackholed.add(("2606:4700::1111", 80))
        net.routes[(PUBLIC_IP, 80)] = ("127.0.0.1", http_server.port)
        _webhook(url="http://dual.example/hook").send(SAMPLE_EVENT)
        assert net.dials == [("2606:4700::1111", 80), (PUBLIC_IP, 80)]
        assert len(http_server.requests) == 1

    def test_https_certificate_failure_sends_no_request(self, net, tmp_path):
        certs = make_certs(tmp_path / "tls", "hooks.example", self_signed=True)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certs.cert_pem, certs.key_pem)
        srv = HTTPFixture(tls=ctx)
        try:
            net.dns["hooks.example"] = [PUBLIC_IP]
            net.routes[(PUBLIC_IP, 443)] = ("127.0.0.1", srv.port)
            hook = _webhook(url="https://hooks.example/hook", auth_token="secret-token")
            with pytest.raises(requests.exceptions.SSLError):
                hook.send(SAMPLE_EVENT)
            assert net.dials == [(PUBLIC_IP, 443)]
            assert srv.requests == []
        finally:
            srv.close()


def test_https_pinned_connection_verifies_hostname(net, tmp_path):
    """Positive HTTPS path: the TCP connection goes to the validated address
    while SNI and certificate checks use the configured hostname."""
    import safe_http

    certs = make_certs(tmp_path / "tls-ok", "hooks.example")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certs.cert_pem, certs.key_pem)
    srv = HTTPFixture(tls=ctx)
    try:
        net.dns["hooks.example"] = [PUBLIC_IP]
        net.routes[(PUBLIC_IP, 443)] = ("127.0.0.1", srv.port)
        resp = safe_http.send("POST", "https://hooks.example/x", json={"a": 1},
                              verify=str(certs.ca_pem))
        assert resp.status_code == 200
        assert net.dials == [(PUBLIC_IP, 443)]
        assert srv.requests[0].headers["Host"] == "hooks.example"
    finally:
        srv.close()


class TestNtfyDestinations:
    def _make(self, **cfg):
        from ntfy import NtfyNotifier

        return NtfyNotifier({"name": "phone", "topic": "pond", "token": "tk_secret", **cfg})

    def test_self_hosted_lan_ntfy_with_allow_internal(self, net, http_server, snapshot_dir):
        net.dns["ntfy.lan"] = [LAN_IP]
        net.routes[(LAN_IP, 80)] = ("127.0.0.1", http_server.port)
        snap = write_image(snapshot_dir / "frame.png", "PNG")
        self._make(server="http://ntfy.lan", allow_internal=True).send(
            {**SAMPLE_EVENT, "snapshot_path": str(snap)},
        )
        req = http_server.requests[0]
        assert (req.method, req.path) == ("PUT", "/pond")
        assert req.headers["Authorization"] == "Bearer tk_secret"
        assert req.body == snap.read_bytes()

    def test_lan_ntfy_without_allow_internal_sends_no_token(self, net, http_server):
        net.dns["ntfy.lan"] = [LAN_IP]
        net.routes[(LAN_IP, 80)] = ("127.0.0.1", http_server.port)
        with pytest.raises(UnsafeURLError):
            self._make(server="http://ntfy.lan").send(SAMPLE_EVENT)
        assert http_server.requests == [] and net.dials == []

    def test_ntfy_server_on_docker_network_disabled(self, net):
        notifier = self._make(server="http://172.24.0.9", allow_internal=True)
        notifier.send(SAMPLE_EVENT)
        assert net.dials == []

    def test_ntfy_digest_also_validated(self, net, http_server):
        net.dns["ntfy.example"] = ["127.0.0.1"]
        net.routes[("127.0.0.1", 80)] = ("127.0.0.1", http_server.port)
        with pytest.raises(UnsafeURLError):
            self._make(server="http://ntfy.example", allow_internal=True).send(DIGEST_REPORT)
        assert http_server.requests == []


class TestDiscordDestinations:
    def test_discord_never_reaches_lan(self, net, http_server):
        from discord import DiscordNotifier

        net.dns["discord.example"] = [LAN_IP]
        net.routes[(LAN_IP, 80)] = ("127.0.0.1", http_server.port)
        notifier = DiscordNotifier({"webhook_url": "http://discord.example/api/webhooks/1/t",
                                    "allow_internal": True})
        notifier.send(SAMPLE_EVENT)  # disabled: allow_internal is not a Discord option
        assert http_server.requests == [] and net.dials == []

    def test_discord_hostname_resolving_private_refused(self, net, http_server):
        from discord import DiscordNotifier

        net.dns["discord.example"] = [LAN_IP]
        net.routes[(LAN_IP, 80)] = ("127.0.0.1", http_server.port)
        notifier = DiscordNotifier({"webhook_url": "http://discord.example/api/webhooks/1/t"})
        with pytest.raises(UnsafeURLError):
            notifier.send(SAMPLE_EVENT)
        assert http_server.requests == []


# ── Snapshot attachments ────────────────────────────────────────────────────


class TestSnapshotAttachments:
    def test_valid_jpeg_and_png_inside_root(self, snapshot_dir):
        from snapshot_utils import load_snapshot

        jpg = write_image(snapshot_dir / "cam" / "a.jpg")
        png = write_image(snapshot_dir / "test-fish.png", "PNG")
        a = load_snapshot(str(jpg))
        b = load_snapshot(str(png))
        assert a is not None and a.data == jpg.read_bytes() and a.format == "JPEG"
        assert b is not None and b.filename == "test-fish.png" and b.format == "PNG"

    def test_image_outside_root_refused(self, snapshot_dir, tmp_path):
        from snapshot_utils import load_snapshot

        outside = write_image(tmp_path / "elsewhere" / "frame.jpg")
        assert load_snapshot(str(outside)) is None

    def test_traversal_out_of_root_refused(self, snapshot_dir, tmp_path):
        from snapshot_utils import load_snapshot

        write_image(tmp_path / "secret.jpg")
        assert load_snapshot(str(snapshot_dir / ".." / "secret.jpg")) is None

    def test_symlink_inside_root_to_outside_file_refused(self, snapshot_dir, tmp_path):
        from snapshot_utils import load_snapshot

        secret = tmp_path / "scarguard.yml"
        secret.write_text("notifications: {smtp_pass: hunter2}\n")
        (snapshot_dir / "evil.jpg").symlink_to(secret)
        outside_img = write_image(tmp_path / "other" / "real.jpg")
        (snapshot_dir / "evil2.jpg").symlink_to(outside_img)
        assert load_snapshot(str(snapshot_dir / "evil.jpg")) is None
        assert load_snapshot(str(snapshot_dir / "evil2.jpg")) is None

    def test_symlinked_root_is_resolved(self, tmp_path, monkeypatch):
        from snapshot_utils import load_snapshot

        real_root = tmp_path / "real-snapshots"
        img = write_image(real_root / "frame.jpg")
        link_root = tmp_path / "snapshots-link"
        link_root.symlink_to(real_root)
        monkeypatch.setenv("SNAPSHOT_DIR", str(link_root))
        assert load_snapshot(str(link_root / "frame.jpg")) is not None
        assert load_snapshot(str(img)) is not None

    @pytest.mark.parametrize("name,content", [
        ("fake.jpg", b"\xff\xd8\xff" + b"\x00" * 100),            # JPEG magic, not an image
        ("config.jpg", b"redis:\n  password: hunter2\n"),          # text renamed .jpg
        ("frame.txt", None),                                      # real JPEG, wrong suffix
        ("frame.yml", None),
    ])
    def test_non_image_or_wrong_suffix_refused(self, snapshot_dir, name, content):
        from snapshot_utils import load_snapshot

        path = snapshot_dir / name
        if content is None:
            write_image(path)
        else:
            path.write_bytes(content)
        assert load_snapshot(str(path)) is None

    def test_decompression_bomb_refused(self, snapshot_dir, monkeypatch):
        import snapshot_utils

        img = write_image(snapshot_dir / "big.png", "PNG", size=(400, 300))
        monkeypatch.setattr(snapshot_utils, "MAX_SNAPSHOT_PIXELS", 100_000)
        assert snapshot_utils.load_snapshot(str(img)) is None

    def test_content_must_match_suffix(self, snapshot_dir):
        from snapshot_utils import load_snapshot

        png_as_jpg = write_image(snapshot_dir / "frame.jpg", "PNG")
        assert load_snapshot(str(png_as_jpg)) is None

    def test_directory_and_fifo_refused(self, snapshot_dir):
        from snapshot_utils import load_snapshot

        (snapshot_dir / "dir.jpg").mkdir()
        assert load_snapshot(str(snapshot_dir / "dir.jpg")) is None
        if hasattr(os, "mkfifo"):
            os.mkfifo(snapshot_dir / "pipe.jpg")
            assert load_snapshot(str(snapshot_dir / "pipe.jpg")) is None

    def test_discord_does_not_upload_outside_file(self, net, http_server, snapshot_dir, tmp_path):
        from discord import DiscordNotifier

        net.dns["discord.example"] = [PUBLIC_IP]
        net.routes[(PUBLIC_IP, 80)] = ("127.0.0.1", http_server.port)
        secret = tmp_path / "scarguard.yml"
        secret.write_text("smtp_pass: hunter2\n")
        (snapshot_dir / "frame.jpg").symlink_to(secret)
        DiscordNotifier({"webhook_url": "http://discord.example/api/webhooks/1/t"}).send(
            {**SAMPLE_EVENT, "snapshot_path": str(snapshot_dir / "frame.jpg")},
        )
        req = http_server.requests[0]
        assert req.headers["Content-Type"] == "application/json"
        assert b"hunter2" not in req.body

    def test_ntfy_does_not_upload_arbitrary_file(self, net, http_server, tmp_path, snapshot_dir):
        from ntfy import NtfyNotifier

        net.dns["ntfy.example"] = [PUBLIC_IP]
        net.routes[(PUBLIC_IP, 80)] = ("127.0.0.1", http_server.port)
        secret = tmp_path / "secret.key"
        secret.write_bytes(b"-----BEGIN PRIVATE KEY-----")
        NtfyNotifier({"server": "http://ntfy.example", "topic": "pond"}).send(
            {**SAMPLE_EVENT, "snapshot_path": str(secret)},
        )
        req = http_server.requests[0]
        assert req.method == "POST" and "Filename" not in req.headers
        assert b"PRIVATE KEY" not in req.body


# ── SMTP: verified TLS, STARTTLS on nonstandard ports, plaintext opt-in ─────

SMTP_HOST = "smtp.lan.test"


@pytest.fixture()
def certs(tmp_path: Path):
    return make_certs(tmp_path / "smtp-tls", SMTP_HOST)


@pytest.fixture()
def smtp_servers():
    started: list[SMTPFixture] = []

    def _start(*args, **kwargs) -> SMTPFixture:
        srv = SMTPFixture(*args, **kwargs)
        started.append(srv)
        return srv

    yield _start
    for srv in started:
        srv.close()


def _email(port: int, **cfg):
    from email_notifier import EmailNotifier

    return EmailNotifier({
        "name": "mail",
        "smtp_host": SMTP_HOST,
        "smtp_port": port,
        "smtp_user": "pond@example.com",
        "smtp_pass": "hunter2",
        "to_addresses": ["owner@example.com"],
        "include_snapshot": False,
        "allow_internal": True,
        **cfg,
    })


def _route_smtp(net, srv: SMTPFixture, port: int, ip: str = LAN_IP) -> None:
    net.dns[SMTP_HOST] = [ip]
    net.routes[(ip, port)] = ("127.0.0.1", srv.port)


class TestSMTPTransport:
    @pytest.mark.parametrize("port", [587, 2525, 25])
    def test_starttls_verified_with_trusted_ca(self, net, certs, smtp_servers, port):
        srv = smtp_servers(certs)
        _route_smtp(net, srv, port)
        _email(port, smtp_ca_file=str(certs.ca_pem)).send(SAMPLE_EVENT)

        sess = srv.sessions[0]
        assert sess.tls is True
        assert sess.auth == ("pond@example.com", "hunter2")
        assert sess.auth_over_tls is True
        assert b"Great Blue Heron" in sess.messages[0]
        assert net.dials == [(LAN_IP, port)]

    def test_untrusted_certificate_fails_before_credentials(self, net, certs, smtp_servers):
        srv = smtp_servers(certs)
        _route_smtp(net, srv, 2525)
        with pytest.raises(ssl.SSLCertVerificationError):
            _email(2525).send(SAMPLE_EVENT)  # no smtp_ca_file: private CA not trusted
        sess = srv.sessions[0]
        assert sess.auth is None and sess.messages == []

    def test_hostname_mismatch_fails(self, net, tmp_path, smtp_servers):
        other = make_certs(tmp_path / "other", "mail.someone-else.test")
        srv = smtp_servers(other)
        _route_smtp(net, srv, 587)
        with pytest.raises(ssl.SSLCertVerificationError):
            _email(587, smtp_ca_file=str(other.ca_pem)).send(SAMPLE_EVENT)
        assert srv.sessions[0].auth is None

    def test_no_starttls_refused_without_opt_in(self, net, smtp_servers):
        from email_notifier import SMTPTransportError

        srv = smtp_servers(None, starttls=False)
        _route_smtp(net, srv, 25)
        with pytest.raises(SMTPTransportError, match="smtp_insecure_plaintext"):
            _email(25).send(SAMPLE_EVENT)
        sess = srv.sessions[0]
        assert "AUTH" not in sess.commands and sess.messages == []

    def test_plaintext_opt_in_preserves_lan_relay(self, net, smtp_servers):
        srv = smtp_servers(None, starttls=False)
        _route_smtp(net, srv, 25)
        _email(25, smtp_insecure_plaintext=True).send(SAMPLE_EVENT)
        sess = srv.sessions[0]
        assert sess.tls is False
        assert sess.auth == ("pond@example.com", "hunter2")
        assert len(sess.messages) == 1

    def test_plaintext_opt_in_skips_starttls(self, net, certs, smtp_servers):
        srv = smtp_servers(certs)
        _route_smtp(net, srv, 25)
        _email(25, smtp_insecure_plaintext=True).send(SAMPLE_EVENT)
        assert "STARTTLS" not in srv.sessions[0].commands

    def test_implicit_tls_port_465_verified(self, net, certs, smtp_servers):
        srv = smtp_servers(certs, implicit_tls=True)
        _route_smtp(net, srv, 465)
        _email(465, smtp_ca_file=str(certs.ca_pem)).send(SAMPLE_EVENT)
        sess = srv.sessions[0]
        assert sess.tls and sess.auth_over_tls and len(sess.messages) == 1

    def test_implicit_tls_untrusted_certificate_fails(self, net, certs, smtp_servers):
        srv = smtp_servers(certs, implicit_tls=True)
        _route_smtp(net, srv, 465)
        with pytest.raises(ssl.SSLCertVerificationError):
            _email(465).send(SAMPLE_EVENT)
        assert all(s.auth is None for s in srv.sessions)

    def test_digest_uses_same_transport(self, net, certs, smtp_servers):
        srv = smtp_servers(certs)
        _route_smtp(net, srv, 587)
        _email(587, smtp_ca_file=str(certs.ca_pem)).send(DIGEST_REPORT)
        assert srv.sessions[0].tls and len(srv.sessions[0].messages) == 1

    def test_snapshot_attached_only_when_valid(self, net, certs, smtp_servers, snapshot_dir, tmp_path):
        srv = smtp_servers(certs)
        _route_smtp(net, srv, 587)
        good = write_image(snapshot_dir / "frame.jpg")
        notifier = _email(587, smtp_ca_file=str(certs.ca_pem), include_snapshot=True)
        notifier.send({**SAMPLE_EVENT, "snapshot_path": str(good)})
        secret = tmp_path / "creds.jpg"
        secret.write_text("smtp_pass: hunter2")
        notifier.send({**SAMPLE_EVENT, "snapshot_path": str(secret)})

        with_image, without_image = (s.messages[0] for s in srv.sessions)
        assert b"Content-ID: <snapshot>" in with_image
        assert b"Content-ID: <snapshot>" not in without_image
        assert b"aHVudGVyMg" not in without_image  # base64 of the secret never attached


class TestSMTPDestinations:
    @pytest.mark.parametrize("addr", ["127.0.0.1", "169.254.169.254", "172.17.0.1", "172.24.0.3"])
    def test_relay_resolving_to_denied_range_refused(self, net, certs, smtp_servers, addr):
        srv = smtp_servers(certs)
        _route_smtp(net, srv, 587, ip=addr)
        with pytest.raises(UnsafeURLError):
            _email(587, smtp_ca_file=str(certs.ca_pem)).send(SAMPLE_EVENT)
        assert net.dials == [] and srv.sessions == []

    def test_lan_relay_requires_allow_internal(self, net, certs, smtp_servers):
        srv = smtp_servers(certs)
        _route_smtp(net, srv, 587)
        with pytest.raises(UnsafeURLError, match="allow_internal"):
            _email(587, smtp_ca_file=str(certs.ca_pem), allow_internal=False).send(SAMPLE_EVENT)
        assert srv.sessions == []

    @pytest.mark.parametrize("host", ["127.0.0.1", "redis", "172.17.0.1", "localhost"])
    def test_static_denied_relay_disables_channel(self, net, host):
        from email_notifier import EmailNotifier

        notifier = EmailNotifier({
            "smtp_host": host, "smtp_port": 25, "smtp_pass": "hunter2",
            "to_addresses": ["a@example.com"], "allow_internal": True,
        })
        notifier.send(SAMPLE_EVENT)
        assert net.dials == [] and net.lookups == []

    def test_missing_ca_file_disables_channel(self, net, tmp_path, smtp_servers, certs):
        srv = smtp_servers(certs)
        _route_smtp(net, srv, 587)
        _email(587, smtp_ca_file=str(tmp_path / "nope.pem")).send(SAMPLE_EVENT)
        assert srv.sessions == [] and net.dials == []

    def test_numeric_string_port_still_works(self, net, certs, smtp_servers):
        srv = smtp_servers(certs)
        _route_smtp(net, srv, 587)
        _email("587", smtp_ca_file=str(certs.ca_pem)).send(SAMPLE_EVENT)
        assert len(srv.sessions[0].messages) == 1

    def test_string_true_is_not_an_opt_in(self, net, smtp_servers):
        srv = smtp_servers(None, starttls=False)
        _route_smtp(net, srv, 25)
        # A quoted "true" in hand-edited YAML must not enable plaintext.
        notifier = _email(25, smtp_insecure_plaintext="true")
        notifier.send(SAMPLE_EVENT)  # disabled at construction (non-bool flag)
        assert srv.sessions == []

    def test_public_relay_does_not_need_allow_internal(self, net, certs, smtp_servers):
        srv = smtp_servers(certs)
        _route_smtp(net, srv, 587, ip=PUBLIC_IP)
        _email(587, smtp_ca_file=str(certs.ca_pem), allow_internal=False).send(SAMPLE_EVENT)
        assert len(srv.sessions[0].messages) == 1


def test_smtp_connection_error_still_propagates_for_retry(net):
    net.dns[SMTP_HOST] = [LAN_IP]  # no route
    with pytest.raises((OSError, smtplib.SMTPException)):
        _email(587).send(SAMPLE_EVENT)
