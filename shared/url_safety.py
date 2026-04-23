"""External-URL validation to block SSRF against internal services.

The notifier accepts webhook URLs (Discord, generic webhook, ntfy) from
config and ``requests.post()`` them directly. Without validation an
admin (or an attacker with a stolen session) can point those URLs at
internal services — Redis on the Docker network, a metadata endpoint,
``http://127.0.0.1:6379/`` — and turn the notifier into a blind SSRF
proxy. Include-snapshot-URL settings can also leak snapshot files to
attacker-controlled receivers.

Call :func:`validate_external_url` at config save time (Pydantic model
validator) and at notifier-channel construction time. Both layers
belt-and-suspenders the check so a raw-YAML edit that bypasses Pydantic
still gets caught before any actual HTTP call.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class UnsafeURLError(ValueError):
    """Raised when a URL resolves to a host in a disallowed range."""


_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


def _is_blocked_address(addr: str) -> bool:
    """True iff *addr* is a literal IP in a disallowed range.

    Disallowed: loopback, link-local, multicast, reserved, and the
    well-known cloud metadata-service addresses."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False

    if ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return True
    if ip.is_private or ip.is_unspecified or ip.is_reserved:
        return True
    # Cloud metadata endpoints (belt-and-suspenders on top of is_link_local).
    blocked_literals = {
        "169.254.169.254",   # AWS / Azure / GCP IMDS
        "fd00:ec2::254",     # EC2 IMDSv2 over IPv6
        "fe80::a9fe:a9fe",
    }
    if str(ip) in blocked_literals:
        return True
    return False


def _resolve(hostname: str) -> list[str]:
    """Return every IP address *hostname* resolves to. Raises on failure.

    Uses getaddrinfo so both A and AAAA records are covered."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"DNS resolution failed for {hostname!r}: {exc}") from exc
    addrs: list[str] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        if family in (socket.AF_INET, socket.AF_INET6):
            ip = sockaddr[0]
            if isinstance(ip, str):
                addrs.append(ip)
    if not addrs:
        raise UnsafeURLError(f"No IPs found for {hostname!r}")
    return addrs


def validate_external_url(url: str, *, allow_internal: bool = False) -> None:
    """Raise :class:`UnsafeURLError` if *url* is not safe to fetch.

    * Scheme must be ``http`` or ``https``.
    * URL must parse and have a hostname.
    * If *allow_internal* is False (the default), every resolved IP for
      the hostname must be a public address. A single private/loopback/
      link-local IP is enough to reject the URL.

    When *allow_internal* is True, only RFC1918 private ranges are
    permitted — for operators who run a home-LAN Home Assistant or
    similar internal webhook receiver. Loopback, link-local, multicast,
    unspecified (0.0.0.0/::), and reserved ranges remain rejected
    (those are never legitimate webhook targets).
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("URL must be a non-empty string")

    parsed = urlparse(url.strip())
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(
            f"URL scheme {parsed.scheme!r} not allowed — must be http or https",
        )
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no hostname")

    # First, handle literal IPs directly — no DNS round-trip, no TOCTOU.
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        if _ip_is_internal(literal_ip):
            if not allow_internal or not _is_rfc1918_private(literal_ip):
                raise UnsafeURLError(
                    f"URL resolves to internal address {host}",
                )
        elif _is_blocked_address(host):
            raise UnsafeURLError(f"URL resolves to blocked address {host}")
        return

    # Hostname — resolve and check every returned IP.
    addrs = _resolve(host)
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_is_internal(ip):
            if not allow_internal or not _is_rfc1918_private(ip):
                raise UnsafeURLError(
                    f"URL {url!r} resolves to internal address {addr}",
                )


def _ip_is_internal(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


def _is_rfc1918_private(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """True iff *ip* is a private-LAN address safe to allow in allow_internal
    mode. Excludes loopback/link-local (which ``is_private`` considers
    private) and multicast/reserved/unspecified."""
    return ip.is_private and not (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )
