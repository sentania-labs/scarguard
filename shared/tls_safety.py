"""Strict validation of the ``tls`` section of scarguard.yml.

One source of truth for both writers of TLS state:

* the web service rejects bad values before saving them (config_model.TLSConfig);
* the Caddy entrypoint (config/caddy_config.py) re-checks them before rendering
  a Caddyfile, because scarguard.yml can also be edited by hand.

Every value checked here is interpolated into a Caddyfile, so the rules are
allowlists, not blocklists: a hostname is DNS labels and dots, a certificate
path is plain file/directory names under /config. Anything else - whitespace,
newlines, braces, quotes, ``..``, other directories - is refused.
"""

from __future__ import annotations

import re
from typing import Any

TLS_MODES: tuple[str, ...] = ("off", "auto", "manual")

# The only directory the caddy container can read user certificates from is
# the scarguard-config volume mounted at /config. The web upload endpoint and
# the README use /config/certs; other sub-directories are accepted so existing
# installs keep working, but every segment is a plain file/directory name.
CONFIG_DIR = "/config"
CERT_DIR = "/config/certs"
DEFAULT_CERT_PATH = f"{CERT_DIR}/cert.pem"
DEFAULT_KEY_PATH = f"{CERT_DIR}/key.pem"

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_IPV4 = re.compile(
    r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(?:\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){3}"
)
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MAX_PATH_SEGMENTS = 4


class TLSValueError(ValueError):
    """A tls value that must not reach the Caddyfile."""


def _hostname_labels(host: str) -> list[str]:
    if len(host) > 253:
        raise TLSValueError("tls.domain is longer than 253 characters")
    labels = host.split(".")
    for label in labels:
        if not _LABEL.fullmatch(label):
            raise TLSValueError(
                "tls.domain must contain only letters, digits, hyphens and dots",
            )
    return labels


def validate_domain(value: Any, *, for_certificate: bool = True) -> str:
    """Return *value* normalised to lower case, or ``""`` when unset.

    With *for_certificate* (``tls.mode: auto``, where the domain becomes the
    Caddy site address and the ACME identifier) only a fully qualified DNS
    hostname is accepted: LDH labels, at least two of them, non-numeric top
    label - no IPs, wildcards, ports or single words.

    Otherwise the domain is used only to build feedback links
    (``https://<domain>``), so a LAN hostname, an IPv4 address and an
    optional ``:port`` are also accepted. Both forms refuse whitespace,
    newlines, braces, quotes and every other character outside the
    allowlist.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TLSValueError("tls.domain must be a string")
    domain = value.strip()
    if not domain:
        return ""
    if any(c.isspace() or not c.isprintable() for c in domain):
        raise TLSValueError("tls.domain must not contain whitespace or newlines")
    domain = domain.lower()

    if for_certificate:
        host = domain[:-1] if domain.endswith(".") else domain
        labels = _hostname_labels(host)
        if len(labels) < 2:
            raise TLSValueError(
                "tls.domain must be a fully qualified hostname for automatic certificates",
            )
        if labels[-1].isdigit():
            raise TLSValueError(
                "tls.domain must be a hostname, not an IP address, for automatic certificates",
            )
        return host

    host, sep, port = domain.partition(":")
    if sep and not (port.isdigit() and 1 <= int(port) <= 65535 and port[0] != "0"):
        raise TLSValueError("tls.domain port must be a number between 1 and 65535")
    if not _IPV4.fullmatch(host):
        labels = _hostname_labels(host)
        if labels[-1].isdigit():
            raise TLSValueError("tls.domain is not a valid hostname or IPv4 address")
    return domain


def validate_cert_path(value: Any, field: str = "cert_path") -> str:
    """Return *value* if it names a file inside ``CONFIG_DIR`` by plain names."""
    if not isinstance(value, str):
        raise TLSValueError(f"tls.{field} must be a string")
    prefix = CONFIG_DIR + "/"
    if not value.startswith(prefix):
        raise TLSValueError(f"tls.{field} must be a file under {CONFIG_DIR}/ (e.g. {CERT_DIR}/)")
    segments = value[len(prefix) :].split("/")
    if len(segments) > _MAX_PATH_SEGMENTS or not all(
        _PATH_SEGMENT.fullmatch(seg) and ".." not in seg for seg in segments
    ):
        raise TLSValueError(
            f"tls.{field} may contain only letters, digits, '.', '_', '-' and '/'",
        )
    return value


def validate_tls(tls: Any) -> dict[str, str]:
    """Validate a whole ``tls`` mapping and return the normalised values.

    Missing keys take their defaults. Raises :class:`TLSValueError` on the
    first invalid value; the message never echoes the value itself.
    """
    if tls is None:
        tls = {}
    if not isinstance(tls, dict):
        raise TLSValueError("tls must be a mapping")
    mode = tls.get("mode", "off")
    if mode is False or mode is None:
        # YAML 1.1 reads an unquoted `off` as boolean false.
        mode = "off"
    if not isinstance(mode, str) or mode.strip().lower() not in TLS_MODES:
        raise TLSValueError("tls.mode must be one of: off, auto, manual")
    mode = mode.strip().lower()
    domain = validate_domain(tls.get("domain", ""), for_certificate=mode == "auto")
    cert_path = validate_cert_path(tls.get("cert_path", DEFAULT_CERT_PATH), "cert_path")
    key_path = validate_cert_path(tls.get("key_path", DEFAULT_KEY_PATH), "key_path")
    if mode == "auto" and not domain:
        raise TLSValueError("tls.domain is required when tls.mode is auto")
    return {"mode": mode, "domain": domain, "cert_path": cert_path, "key_path": key_path}
