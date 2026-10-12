"""HTTP sends pinned to validated addresses.

Every webhook / Discord / ntfy request goes through :func:`send`:

1. the URL is resolved once and every address is checked
   (:func:`url_safety.resolve_url`);
2. the TCP connection is opened to those checked addresses only, while TLS
   SNI, certificate verification and the ``Host`` header still use the
   configured hostname - a second DNS answer can never be used;
3. redirects are never followed (a 3xx is an error), so a receiver cannot
   bounce the request - and its credentials - to an internal target;
4. proxy environment variables are ignored, since a proxy would make the
   connection somewhere other than the validated address.
"""

from __future__ import annotations

import socket
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from url_safety import Destination, UnsafeURLError, connect_pinned, resolve_url
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError


class RedirectRefusedError(UnsafeURLError):
    """The destination answered with a redirect, which is never followed."""


def _pinned_new_conn(conn: HTTPConnection, dest: Destination) -> socket.socket:
    timeout = conn.timeout if isinstance(conn.timeout, (int, float)) else None
    pinned = Destination(host=dest.host, port=conn.port or dest.port, addresses=dest.addresses)
    try:
        sock = connect_pinned(pinned, timeout, conn.source_address)
    except socket.timeout as exc:
        raise ConnectTimeoutError(
            conn, f"Connection to {conn.host} timed out. (connect timeout={timeout})",
        ) from exc
    except OSError as exc:
        raise NewConnectionError(conn, f"Failed to establish a new connection: {exc}") from exc
    for opt in conn.socket_options or ():
        sock.setsockopt(*opt)
    return sock


class _PinnedAdapter(HTTPAdapter):
    """Transport adapter whose connections dial only ``dest.addresses``."""

    def __init__(self, dest: Destination) -> None:
        self._dest = dest
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        super().init_poolmanager(*args, **kwargs)
        dest = self._dest

        class _HTTPConn(HTTPConnection):
            def _new_conn(self) -> socket.socket:
                return _pinned_new_conn(self, dest)

        class _HTTPSConn(HTTPSConnection):
            def _new_conn(self) -> socket.socket:
                return _pinned_new_conn(self, dest)

        class _HTTPPool(HTTPConnectionPool):
            ConnectionCls = _HTTPConn

        class _HTTPSPool(HTTPSConnectionPool):
            ConnectionCls = _HTTPSConn

        self.poolmanager.pool_classes_by_scheme = {"http": _HTTPPool, "https": _HTTPSPool}


def send(
    method: str,
    url: str,
    *,
    allow_internal: bool = False,
    timeout: float = 10,
    **kwargs: Any,
) -> requests.Response:
    """Validate *url*, then send one request pinned to the validated addresses.

    Raises :class:`url_safety.UnsafeURLError` for a disallowed destination or
    a redirect, and the usual ``requests`` exceptions for transport errors.
    """
    dest = resolve_url(url, allow_internal=allow_internal)
    with requests.Session() as session:
        session.trust_env = False
        adapter = _PinnedAdapter(dest)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        resp = session.request(method, url, allow_redirects=False, timeout=timeout, **kwargs)
    if 300 <= resp.status_code < 400:
        resp.close()
        raise RedirectRefusedError(
            f"destination {dest.host!r} answered {resp.status_code} redirect; "
            "redirects are not followed",
        )
    return resp
