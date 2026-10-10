"""Shared notifier test data and local fixture servers.

The fixture servers are real sockets on 127.0.0.1. Destinations in tests use
hostnames answered by :class:`FakeNet`, which stands in for DNS
(``socket.getaddrinfo``) and for the final routing hop
(``url_safety._socket_connect``): a connection to a validated address such
as 192.168.77.10:8080 is delivered to the fixture server listening on a
loopback port. Everything between - URL parsing, resolution, address policy,
pinning, TLS, HTTP and SMTP - runs the production code unchanged, and
FakeNet records every lookup and every address actually dialled.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import socket
import ssl
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

SAMPLE_EVENT = {
    "timestamp": "2026-03-20T06:00:00+00:00",
    "class_name": "great_blue_heron",
    "confidence": 0.87,
    "camera_name": "pond-north",
    "snapshot_path": None,
}

_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CREATE_CONNECTION = socket.create_connection


# ── Fake DNS + routing ──────────────────────────────────────────────────────


class FakeNet:
    """Answers DNS for registered names and routes dials to fixture servers."""

    def __init__(self) -> None:
        self.dns: dict[str, list[str] | Callable[[], list[str]]] = {}
        self.routes: dict[tuple[str, int], tuple[str, int]] = {}
        self.blackholed: set[tuple[str, int]] = set()
        self.lookups: list[str] = []
        self.dials: list[tuple[str, int]] = []

    def getaddrinfo(self, host: Any, port: Any, *args: Any, **kwargs: Any) -> list:
        if isinstance(host, str) and host in self.dns:
            self.lookups.append(host)
            answer = self.dns[host]
            ips = answer() if callable(answer) else answer
            return [
                (
                    socket.AF_INET6 if ":" in ip else socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    (ip, port or 0, 0, 0) if ":" in ip else (ip, port or 0),
                )
                for ip in ips
            ]
        return _REAL_GETADDRINFO(host, port, *args, **kwargs)

    def connect(
        self,
        address: tuple[str, int],
        timeout: float | None,
        source_address: tuple[str, int] | None,
    ) -> socket.socket:
        self.dials.append((address[0], address[1]))
        if (address[0], address[1]) in self.blackholed:
            raise socket.timeout("timed out")
        target = self.routes.get((address[0], address[1]))
        if target is None:
            raise ConnectionRefusedError(f"test network has no route to {address}")
        return _REAL_CREATE_CONNECTION(target, timeout)


@pytest.fixture()
def net(monkeypatch: pytest.MonkeyPatch) -> FakeNet:
    import url_safety

    fake = FakeNet()
    monkeypatch.setattr(url_safety.socket, "getaddrinfo", fake.getaddrinfo)
    monkeypatch.setattr(url_safety, "_socket_connect", fake.connect)
    return fake


# ── Certificates ────────────────────────────────────────────────────────────


@dataclass
class CertSet:
    ca_pem: Path
    cert_pem: Path
    key_pem: Path


def make_certs(tmp: Path, hostname: str, *, self_signed: bool = False) -> CertSet:
    """A CA plus a server certificate for *hostname* (or a lone self-signed one)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = dt.datetime.now(dt.timezone.utc)

    def _name(cn: str) -> x509.Name:
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("ScarGuard test CA"))
        .issuer_name(_name("ScarGuard test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(hostname))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False,
        )
    )
    if self_signed:
        cert = builder.issuer_name(_name(hostname)).sign(key, hashes.SHA256())
    else:
        cert = (
            builder.issuer_name(ca_cert.subject)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )
    tmp.mkdir(parents=True, exist_ok=True)
    certs = CertSet(tmp / "ca.pem", tmp / "cert.pem", tmp / "key.pem")
    certs.ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    certs.cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    certs.key_pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return certs


def _server_context(certs: CertSet) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certs.cert_pem, certs.key_pem)
    return ctx


# ── HTTP fixture server ─────────────────────────────────────────────────────


@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class HTTPFixture:
    def __init__(self, tls: ssl.SSLContext | None = None) -> None:
        self.requests: list[RecordedRequest] = []
        self.responses: list[tuple[int, dict[str, str]]] = []
        self.tls_errors: list[str] = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                fixture.requests.append(
                    RecordedRequest(self.command, self.path, dict(self.headers.items()), body),
                )
                status, headers = fixture.responses.pop(0) if fixture.responses else (200, {})
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            do_GET = do_POST = do_PUT = _handle

            def log_message(self, *args: Any) -> None:
                pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def get_request(self) -> tuple[socket.socket, Any]:
                sock, addr = super().get_request()
                if tls is None:
                    return sock, addr
                try:
                    return tls.wrap_socket(sock, server_side=True), addr
                except (ssl.SSLError, OSError) as exc:
                    fixture.tls_errors.append(str(exc))
                    sock.close()
                    raise

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def http_server() -> Iterator[HTTPFixture]:
    srv = HTTPFixture()
    yield srv
    srv.close()


# ── SMTP fixture server ─────────────────────────────────────────────────────


@dataclass
class SMTPSession:
    tls: bool = False
    tls_error: str | None = None
    auth: tuple[str, str] | None = None
    auth_over_tls: bool | None = None
    messages: list[bytes] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)


class SMTPFixture:
    """Minimal SMTP server: EHLO, STARTTLS, AUTH PLAIN, MAIL/RCPT/DATA, QUIT."""

    def __init__(
        self,
        certs: CertSet | None,
        *,
        starttls: bool = True,
        implicit_tls: bool = False,
    ) -> None:
        self.sessions: list[SMTPSession] = []
        self._ctx = _server_context(certs) if certs else None
        self._starttls = starttls and self._ctx is not None
        self._implicit = implicit_tls
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn: socket.socket) -> None:
        sess = SMTPSession()
        self.sessions.append(sess)
        conn.settimeout(5)
        try:
            if self._implicit:
                assert self._ctx is not None
                try:
                    conn = self._ctx.wrap_socket(conn, server_side=True)
                except (ssl.SSLError, OSError) as exc:
                    sess.tls_error = str(exc)
                    return
                sess.tls = True
            rfile = conn.makefile("rb")

            def reply(line: str) -> None:
                conn.sendall(line.encode() + b"\r\n")

            reply("220 fixture ESMTP")
            while True:
                raw = rfile.readline()
                if not raw:
                    return
                line = raw.decode().rstrip("\r\n")
                sess.commands.append(line.split(" ", 1)[0].upper())
                verb = line.split(" ", 1)[0].upper()
                if verb in ("EHLO", "HELO"):
                    exts = ["fixture"]
                    if self._starttls and not sess.tls:
                        exts.append("STARTTLS")
                    exts.append("AUTH PLAIN")
                    for ext in exts[:-1]:
                        reply(f"250-{ext}")
                    reply(f"250 {exts[-1]}")
                elif verb == "STARTTLS" and self._starttls and not sess.tls:
                    assert self._ctx is not None
                    reply("220 go ahead")
                    try:
                        conn = self._ctx.wrap_socket(conn, server_side=True)
                    except (ssl.SSLError, OSError) as exc:
                        sess.tls_error = str(exc)
                        return
                    sess.tls = True
                    rfile = conn.makefile("rb")
                elif verb == "AUTH":
                    parts = line.split(" ")
                    decoded = base64.b64decode(parts[2]).split(b"\0")
                    sess.auth = (decoded[1].decode(), decoded[2].decode())
                    sess.auth_over_tls = sess.tls
                    reply("235 ok")
                elif verb == "DATA":
                    reply("354 end with .")
                    buf = io.BytesIO()
                    while True:
                        chunk = rfile.readline()
                        if chunk in (b".\r\n", b""):
                            break
                        buf.write(chunk)
                    sess.messages.append(buf.getvalue())
                    reply("250 queued")
                elif verb == "QUIT":
                    reply("221 bye")
                    return
                else:
                    reply("250 ok")
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=2)


# ── Snapshot images ─────────────────────────────────────────────────────────


def write_image(path: Path, fmt: str = "JPEG", size: tuple[int, int] = (32, 24)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (30, 120, 200)).save(path, format=fmt)
    return path


@pytest.fixture()
def snapshot_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "snapshots"
    root.mkdir()
    monkeypatch.setenv("SNAPSHOT_DIR", str(root))
    return root
