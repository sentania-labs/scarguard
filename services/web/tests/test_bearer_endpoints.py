"""Bearer-auth CI sweep — verify every Bearer-accessible endpoint returns non-401.

Enumerates the endpoints documented in the README's "Key endpoints available
with Bearer auth" section and confirms:
  1. A valid Bearer token yields a non-401 response (200, 500, etc. are all
     acceptable — the test is about the auth gate, not the endpoint logic).
  2. A missing token yields 401 (confirms auth is actually enforced).

SSE stream endpoints will fail to connect to Redis in the test environment,
so they may return 500 or similar — that's fine, as long as the auth layer
lets the request through.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

# ── Config with auth enabled ─────────────────────────────────────────────────

_AUTH_CFG: dict[str, Any] = {
    "system": {
        "armed": True,
        "log_level": "info",
        "auth": {"enabled": True},
    },
    "cameras": [
        {
            "name": "pond-north",
            "rtsp_url": "rtsp://localhost/test",
            "enabled": True,
            "resolution": 720,
        }
    ],
    "detection": {
        "model_path": "/models/best.pt",
        "confidence_threshold": 0.25,
        "target_classes": ["great_blue_heron"],
        "cooldown_seconds": 30,
        "frame_skip": 2,
    },
    "redis": {"host": "localhost", "port": 6379},
    "notifications": {},
}

# The token value the fake validator will accept.
_VALID_TOKEN = "test-bearer-token-for-ci-sweep"

# User dict returned by the fake API token validator.
_TOKEN_USER: dict[str, Any] = {
    "token_id": 1,
    "user_id": 1,
    "username": "api-admin",
    "is_admin": 1,
    "role": "admin",
    "disabled": 0,
}


@pytest.fixture()
def bearer_client(monkeypatch):
    """TestClient with auth enabled, API-token validation faked."""
    cfg = copy.deepcopy(_AUTH_CFG)

    monkeypatch.setattr("config_store.load", lambda: cfg)
    monkeypatch.setattr("config_store.load_cached", lambda **_kw: cfg)
    monkeypatch.setattr("config_store.save", lambda _cfg: None)
    monkeypatch.setattr("config_store.set_armed", lambda _armed: None)
    monkeypatch.setattr("db.get_latest_event", lambda: None)
    monkeypatch.setattr("db.count_events", lambda **_kw: 0)
    monkeypatch.setattr("db.get_events", lambda **_kw: [])
    monkeypatch.setattr("db.get_latest_snapshots_by_camera", lambda: {})

    import auth as auth_module

    def _fake_validate_api_token(_db, raw_token: str):
        if raw_token == _VALID_TOKEN:
            return _TOKEN_USER.copy()
        return None

    monkeypatch.setattr(auth_module, "validate_api_token", _fake_validate_api_token)
    # Ensure users_exist returns True so setup redirect doesn't fire.
    monkeypatch.setattr(auth_module, "users_exist", lambda _p: True)

    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app, raise_server_exceptions=False)


# ── Endpoint catalogue ───────────────────────────────────────────────────────
#
# From the README "Key endpoints available with Bearer auth":
#   /events           — detection event log
#   /config           — read or update system configuration
#   /about            — version, build date, component health
#   /events/stream    — SSE stream of live detection events
#   /admin/stats/stream — SSE stream of system resource metrics
#   /admin/logs/stream  — SSE stream of service logs
#
# /health is public (no auth required) — tested separately.

_BEARER_ENDPOINTS: list[tuple[str, str]] = [
    ("GET", "/events"),
    ("GET", "/config"),
    ("GET", "/about"),
    ("GET", "/events/stream"),
    ("GET", "/admin/stats/stream"),
    ("GET", "/admin/logs/stream"),
]


# ── Tests ────────────────────────────────────────────────────────────────────


class TestBearerAuthAccepted:
    """With a valid Bearer token, every documented endpoint must not return 401."""

    @pytest.mark.parametrize(
        "method,path",
        _BEARER_ENDPOINTS,
        ids=[f"{m} {p}" for m, p in _BEARER_ENDPOINTS],
    )
    def test_valid_token_is_not_401(
        self, bearer_client, method: str, path: str,
    ) -> None:
        resp = bearer_client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
        )
        assert resp.status_code != 401, (
            f"{method} {path} returned 401 with a valid Bearer token"
        )


class TestBearerAuthEnforced:
    """Without a Bearer token (or session cookie), the same endpoints must return 401."""

    @pytest.mark.parametrize(
        "method,path",
        _BEARER_ENDPOINTS,
        ids=[f"{m} {p}" for m, p in _BEARER_ENDPOINTS],
    )
    def test_missing_token_is_401(
        self, bearer_client, method: str, path: str,
    ) -> None:
        # Send Accept: application/json so the middleware returns 401 JSON
        # instead of a 302 redirect to /login (which is the HTML-path).
        resp = bearer_client.request(
            method,
            path,
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 401, (
            f"{method} {path} returned {resp.status_code} without auth, expected 401"
        )


class TestHealthPublic:
    """/health is public and should work with or without a Bearer token."""

    def test_health_without_token(self, bearer_client) -> None:
        resp = bearer_client.get("/health")
        assert resp.status_code == 200

    def test_health_with_token(self, bearer_client) -> None:
        resp = bearer_client.get(
            "/health",
            headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
        )
        assert resp.status_code == 200
