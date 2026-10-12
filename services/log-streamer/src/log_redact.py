"""Credential redaction for log text.

Applied at the source (a filter on each service's root log handlers) so a
credential never reaches stdout, the log-streamer's Redis buffer or the
admin log viewer. The log-streamer keeps a byte-identical copy in
``services/log-streamer/src/log_redact.py`` because its image does not ship
``shared/``; keep the two in sync.

Masks:

* RTSP URLs down to scheme plus host/port, removing userinfo, paths and queries;
* Discord webhook tokens (``/api/webhooks/<id>/<token>``);
* ``Bearer``/``Basic`` authorization values;
* ``key=value`` / ``key: value`` pairs whose key names a credential
  (password, token, api_key, api_secret, secret, smtp_pass, ...), including
  query-string parameters such as ``?token=``;
* the value after a ``--roboflow-key`` command-line flag.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

MASK = "***"

# Prefixes are bounded and anchored at a word start so a long run of
# ``-``/``_``/letters cannot make the scan quadratic (this runs on every line).
_URL_USERINFO_RE = re.compile(
    r"(?<![A-Za-z0-9+.\-])(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]{0,31}://)"
    r"(?:(?P<user>[^:/?#@\s]*):)?(?P<secret>[^/?#@\s]+)@",
)
_RTSP_URL_RE = re.compile(r"(?<![A-Za-z0-9+.-])rtsps?://[^\s\"'<>]+", re.IGNORECASE)
_DISCORD_RE = re.compile(
    r"(?P<prefix>/api/webhooks/\d+/)[A-Za-z0-9_\-]+",
)
_AUTH_SCHEME_RE = re.compile(r"(?P<prefix>\b(?:Bearer|Basic)\s+)[A-Za-z0-9._~+/=\-]+")
_KEY_VALUE_RE = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_\-])[\"']?"
    r"(?:[A-Za-z_\-]{0,32}?(?:password|passwd|secret|token|api[_\-]?key|smtp_pass|access[_\-]?key))"
    r"[\"']?\s*[:=]\s*[\"']?)"
    r"(?P<value>[^\s\"'&,;}\]]+)",
    re.IGNORECASE,
)
_FLAG_RE = re.compile(r"(?P<prefix>--roboflow-key(?:\s+|=))(?P<value>[^\s]+)")


def _userinfo(match: re.Match[str]) -> str:
    user = match.group("user")
    if user is None:
        # ``scheme://secret@host`` - the single field may be a token.
        return f"{match.group('scheme')}{MASK}@"
    return f"{match.group('scheme')}{user}:{MASK}@"


def _rtsp_endpoint(match: re.Match[str]) -> str:
    """Keep only the non-secret scheme, host and port of an RTSP URL."""
    try:
        parsed = urlsplit(match.group(0))
    except ValueError:
        return f"rtsp://{MASK}"
    if parsed.hostname is None:
        return f"{parsed.scheme}://{MASK}"
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = ""
    return f"{parsed.scheme}://{host}{port}"


def redact_text(text: str) -> str:
    """Return *text* with credential-shaped substrings masked."""
    if not text:
        return text
    out = _RTSP_URL_RE.sub(_rtsp_endpoint, text)
    out = _URL_USERINFO_RE.sub(_userinfo, out)
    out = _DISCORD_RE.sub(lambda m: f"{m.group('prefix')}{MASK}", out)
    out = _AUTH_SCHEME_RE.sub(lambda m: f"{m.group('prefix')}{MASK}", out)
    out = _FLAG_RE.sub(lambda m: f"{m.group('prefix')}{MASK}", out)
    out = _KEY_VALUE_RE.sub(
        lambda m: m.group(0) if m.group("value") == MASK else f"{m.group('prefix')}{MASK}",
        out,
    )
    return out


class RedactingFilter(logging.Filter):
    """Rewrite each record's message (and traceback text) with secrets masked."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        return True


def install(logger: logging.Logger | None = None) -> None:
    """Attach :class:`RedactingFilter` to every handler of *logger* (root by default)."""
    target = logger or logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
