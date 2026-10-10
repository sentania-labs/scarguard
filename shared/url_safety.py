"""Destination validation for outbound notification traffic (SSRF defence).

The notifier sends to operator-configured destinations - Discord webhook
URLs, generic webhooks, ntfy servers and SMTP relays - and attaches
credentials (bearer tokens, basic auth, SMTP passwords) to those requests.
Without validation an admin (or an attacker with a stolen session) could
point a channel at Redis on the Docker network, a cloud metadata endpoint
or ``127.0.0.1`` and turn the notifier into an SSRF proxy that also hands
over the channel's credentials.

Two layers use this module:

* **Save time** (web ``NotificationsConfig`` validator) and **notifier
  construction** call :func:`channel_destination_errors`. These checks are
  static - scheme, port, literal IPs, ScarGuard's own Docker service names -
  and never touch the network, so saving the form works while DNS is down.
* **Send time** calls :func:`resolve_url` / :func:`resolve_host`. The host is
  resolved exactly once, *every* returned address is checked, and the
  connection is then made to those validated addresses with
  :func:`connect_pinned` - so a DNS answer that changes between the check
  and the connect (DNS rebinding) cannot redirect the request.

Address policy (:func:`check_address`):

* Always denied, even with ``allow_internal``: loopback, link-local
  (including the 169.254.169.254 metadata service), multicast, unspecified,
  reserved, the default Docker bridge ``172.17.0.0/16``, ScarGuard's compose
  network ``172.24.0.0/16`` and known cloud metadata addresses.
* Allowed only with an explicit per-channel ``allow_internal: true``: LAN
  ranges 10/8, 172.16/12, 192.168/16, 100.64/10 (CGNAT, e.g. Tailscale) and
  IPv6 ULA fc00::/7.
* Everything else must be a globally routable address.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import unicodedata
from dataclasses import dataclass
from typing import Any, Union
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


class UnsafeURLError(ValueError):
    """Raised when a destination is malformed or resolves to a disallowed address."""


_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Networks no notification may reach, whatever allow_internal says.
_ALWAYS_DENIED_NETWORKS: tuple[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, str], ...] = (
    (ipaddress.ip_network("172.17.0.0/16"), "the default Docker bridge network"),
    (ipaddress.ip_network("172.24.0.0/16"), "the ScarGuard compose network"),
    (ipaddress.ip_network("100.100.100.200/32"), "a cloud metadata service"),
    (ipaddress.ip_network("168.63.129.16/32"), "a cloud metadata service"),
    (ipaddress.ip_network("192.0.0.192/32"), "a cloud metadata service"),
    (ipaddress.ip_network("fd00:ec2::254/128"), "a cloud metadata service"),
    (ipaddress.ip_network("fec0::/10"), "deprecated IPv6 site-local space"),
)

# Private LAN ranges an operator may opt into with ``allow_internal: true``.
_LAN_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fc00::/7"),
)

# Hostnames that only ever name local or metadata services. Docker service
# names from docker-compose.yml resolve to the compose network inside the
# stack; rejecting them statically gives the operator feedback at save time
# (send time would refuse them anyway by address).
_DENIED_HOSTNAMES: frozenset[str] = frozenset({
    "localhost",
    "metadata",
    "metadata.google.internal",
    "instance-data",
    "host.docker.internal",
    "gateway.docker.internal",
    "volume-init",
    "redis",
    "caddy",
    "detector",
    "web",
    "notifier",
    "deterrent",
    "off-watchdog",
    "backup",
    "docker-socket-proxy",
    "log-streamer",
    "config-api",
    "training-controller",
    "trainer",
})

# Values the validator cannot inspect: the web form ships secrets as the
# redaction placeholder (config_redact.REDACTED_PLACEHOLDER) and the stored
# file holds them encrypted (secret_box.PREFIX). The notifier validates the
# decrypted value before it sends anything.
_OPAQUE_PREFIXES: tuple[str, ...] = ("***REDACTED***", "enc:v1:")


@dataclass(frozen=True)
class Destination:
    """A host whose resolved addresses all passed :func:`check_address`.

    ``host`` is kept for TLS SNI, certificate hostname checks and the HTTP
    ``Host`` header; connections go only to ``addresses``.
    """

    host: str
    port: int
    addresses: tuple[str, ...]


def _as_ip(addr: str | IPAddress) -> IPAddress | None:
    if isinstance(addr, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return addr
    try:
        return ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return None


def _numeric_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Interpret legacy IPv4 spellings (``2130706433``, ``0x7f.1``, ``127.1``)
    the way the C resolver would, so they cannot sneak past literal checks."""
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def check_address(addr: str | IPAddress, *, allow_internal: bool = False) -> None:
    """Raise :class:`UnsafeURLError` unless *addr* may receive notifications."""
    ip = _as_ip(addr)
    if ip is None:
        raise UnsafeURLError(f"{addr!r} is not an IP address")
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is not None:
            check_address(embedded, allow_internal=allow_internal)
            if ip.ipv4_mapped is not None:
                return

    if ip.is_loopback:
        raise UnsafeURLError(f"internal address {ip} is loopback")
    if ip.is_link_local:
        raise UnsafeURLError(f"internal address {ip} is link-local (metadata services live here)")
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        raise UnsafeURLError(f"internal address {ip} is not a unicast destination")
    for net, label in _ALWAYS_DENIED_NETWORKS:
        if ip.version == net.version and ip in net:
            raise UnsafeURLError(f"internal address {ip} is {label}")
    if ip.is_global:
        return
    if allow_internal and any(ip.version == n.version and ip in n for n in _LAN_NETWORKS):
        return
    if allow_internal:
        raise UnsafeURLError(f"internal address {ip} is not a permitted LAN range")
    raise UnsafeURLError(
        f"internal address {ip} is private; set allow_internal: true on the "
        "channel to permit a LAN destination",
    )


def check_host_static(host: str, *, allow_internal: bool = False) -> None:
    """Checks that need no DNS: denied names and literal addresses."""
    # NFKC folds fullwidth/compatibility digits the resolver's IDNA step
    # would turn into an IP literal.
    name = unicodedata.normalize("NFKC", host).strip().rstrip(".").lower()
    if not name:
        raise UnsafeURLError("destination has no hostname")
    if name in _DENIED_HOSTNAMES or name.endswith(".localhost"):
        raise UnsafeURLError(f"hostname {name!r} names an internal service")
    literal = _as_ip(name.strip("[]")) or _numeric_ipv4(name)
    if literal is not None:
        check_address(literal, allow_internal=allow_internal)


def _parse_url(url: str) -> tuple[str, int]:
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("URL must be a non-empty string")
    parsed = urlparse(url.strip())
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(
            f"URL scheme {parsed.scheme!r} not allowed - must be http or https",
        )
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no hostname")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("URL has an invalid port") from exc
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return host, port


def check_url_static(url: str, *, allow_internal: bool = False) -> None:
    """Save-time URL check: syntax, scheme, port and static host policy."""
    host, _port = _parse_url(url)
    check_host_static(host, allow_internal=allow_internal)


def _check_port(port: Any) -> int:
    if isinstance(port, str) and port.strip().isdigit():
        port = int(port)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise UnsafeURLError("port must be an integer between 1 and 65535")
    return port


def resolve_host(host: str, port: int, *, allow_internal: bool = False) -> Destination:
    """Resolve *host* once and validate every address it returns.

    A single disallowed address rejects the whole destination - otherwise a
    name answering with both a public and a private IP could still reach the
    private one.
    """
    port = _check_port(port)
    check_host_static(host, allow_internal=allow_internal)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise UnsafeURLError(f"DNS resolution failed for {host!r}: {exc}") from exc
    addrs: list[str] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        if family in (socket.AF_INET, socket.AF_INET6):
            ip = sockaddr[0]
            if isinstance(ip, str) and ip not in addrs:
                addrs.append(ip)
    if not addrs:
        raise UnsafeURLError(f"No IPs found for {host!r}")
    for addr in addrs:
        try:
            check_address(addr, allow_internal=allow_internal)
        except UnsafeURLError as exc:
            raise UnsafeURLError(f"{host!r} resolves to {exc}") from exc
    return Destination(host=host, port=port, addresses=tuple(addrs))


def resolve_url(url: str, *, allow_internal: bool = False) -> Destination:
    """Parse *url*, then :func:`resolve_host` its host and effective port."""
    host, port = _parse_url(url)
    return resolve_host(host, port, allow_internal=allow_internal)


def validate_external_url(url: str, *, allow_internal: bool = False) -> None:
    """Raise :class:`UnsafeURLError` if *url* is not safe to fetch right now.

    Kept for existing callers; new send paths use :func:`resolve_url` so the
    connection is pinned to the addresses that were checked.
    """
    resolve_url(url, allow_internal=allow_internal)


def _socket_connect(
    address: tuple[str, int],
    timeout: float | None,
    source_address: tuple[str, int] | None,
) -> socket.socket:
    """The single place notification traffic opens a TCP connection."""
    return socket.create_connection(address, timeout, source_address)


def connect_pinned(
    dest: Destination,
    timeout: float | None,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Connect to the first reachable validated address of *dest*.

    Like ``socket.create_connection``, a failure or timeout on one address
    moves on to the next; the last error is raised if none connects. Never
    resolves ``dest.host`` again.
    """
    last_exc: OSError | None = None
    for addr in dest.addresses:
        try:
            return _socket_connect((addr, dest.port), timeout, source_address)
        except OSError as exc:  # includes socket.timeout
            last_exc = exc
    raise last_exc or OSError(f"no addresses to connect to for {dest.host!r}")


def _is_opaque(value: str) -> bool:
    return value.startswith(_OPAQUE_PREFIXES)


def channel_destination_errors(
    channel: dict[str, Any],
    *,
    missing_allow_internal: bool = False,
) -> list[str]:
    """Static destination problems for one ``notifications.channels`` entry.

    *missing_allow_internal* is the value assumed when the channel has no
    ``allow_internal`` key. The notifier and whole-document checks use the
    default (absent means off). The web form's partial payload passes True:
    the form does not carry the key, and the stored value is merged back in
    after validation, so absence there means "unchanged", not "off".
    Always-denied addresses fail either way.

    Messages name the channel and field, never the destination value
    (Discord webhook URLs and webhook query strings carry credentials).
    """
    ch_type = str(channel.get("type", "")).lower()
    label = f"channel {str(channel.get('name') or ch_type)!r} ({ch_type})"
    errors: list[str] = []

    for flag in ("allow_internal", "smtp_insecure_plaintext"):
        if flag in channel and not isinstance(channel[flag], bool):
            errors.append(f"{label}: {flag} must be true or false")
    if "allow_internal" in channel:
        allow_internal = channel["allow_internal"] is True
    else:
        allow_internal = missing_allow_internal

    def _url(field: str, value: Any, *, internal: bool) -> None:
        if not isinstance(value, str) or not value.strip() or _is_opaque(value):
            return
        try:
            check_url_static(value, allow_internal=internal)
        except UnsafeURLError as exc:
            errors.append(f"{label}: {field} {exc}")

    if ch_type == "discord":
        if channel.get("allow_internal") is True:
            errors.append(f"{label}: allow_internal is not supported for Discord")
        _url("webhook_url", channel.get("webhook_url"), internal=False)
    elif ch_type == "webhook":
        _url("url", channel.get("url"), internal=allow_internal)
    elif ch_type == "ntfy":
        _url("server", channel.get("server", "https://ntfy.sh"), internal=allow_internal)
    elif ch_type == "email":
        host = channel.get("smtp_host")
        if isinstance(host, str) and host.strip():
            try:
                check_host_static(host, allow_internal=allow_internal)
            except UnsafeURLError as exc:
                errors.append(f"{label}: smtp_host {exc}")
        try:
            _check_port(channel.get("smtp_port", 587))
        except UnsafeURLError as exc:
            errors.append(f"{label}: smtp_port {exc}")
        ca_file = channel.get("smtp_ca_file", "")
        if ca_file and (not isinstance(ca_file, str) or not ca_file.startswith("/")):
            errors.append(f"{label}: smtp_ca_file must be an absolute path")
    return errors
