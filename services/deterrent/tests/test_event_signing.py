"""Tests for the shared HMAC event-signing module.

Lives under the deterrent tests because it's the deterrent service that
can't tolerate signature bypass — fake events there cause physical
actuation. Exercising the module from here also covers the exact import
path deterrent/main.py uses."""

from __future__ import annotations

import base64
import os
from unittest.mock import patch

import pytest
from event_signing import (
    ENV_VAR,
    SIGNATURE_FIELD,
    load_key_from_env,
    sign_event,
    verify_event,
)

KEY_A = b"0" * 32
KEY_B = b"1" * 32


class TestSignAndVerify:
    def test_round_trip(self) -> None:
        event = {"camera_name": "pond-north", "class_name": "heron", "confidence": 0.91}
        signed = sign_event(event, KEY_A)
        assert SIGNATURE_FIELD in signed
        assert verify_event(signed, KEY_A) is True

    def test_signature_depends_on_payload(self) -> None:
        a = sign_event({"x": 1}, KEY_A)
        b = sign_event({"x": 2}, KEY_A)
        assert a[SIGNATURE_FIELD] != b[SIGNATURE_FIELD]

    def test_signature_depends_on_key(self) -> None:
        a = sign_event({"x": 1}, KEY_A)
        b = sign_event({"x": 1}, KEY_B)
        assert a[SIGNATURE_FIELD] != b[SIGNATURE_FIELD]

    def test_verify_rejects_wrong_key(self) -> None:
        signed = sign_event({"x": 1}, KEY_A)
        assert verify_event(signed, KEY_B) is False

    def test_verify_rejects_tampered_payload(self) -> None:
        signed = sign_event({"class_name": "squirrel"}, KEY_A)
        signed["class_name"] = "heron"  # attacker swap
        assert verify_event(signed, KEY_A) is False

    def test_verify_rejects_missing_sig(self) -> None:
        assert verify_event({"x": 1}, KEY_A) is False

    def test_verify_rejects_non_string_sig(self) -> None:
        assert verify_event({"x": 1, SIGNATURE_FIELD: 42}, KEY_A) is False

    def test_verify_rejects_empty_dict(self) -> None:
        assert verify_event({}, KEY_A) is False

    def test_key_order_does_not_affect_signature(self) -> None:
        """Canonical JSON sorts keys, so reordered dicts must verify identically."""
        a = sign_event({"camera": "pond", "class": "heron"}, KEY_A)
        b = sign_event({"class": "heron", "camera": "pond"}, KEY_A)
        assert a[SIGNATURE_FIELD] == b[SIGNATURE_FIELD]

    def test_sign_is_non_mutating(self) -> None:
        event = {"x": 1}
        sign_event(event, KEY_A)
        assert SIGNATURE_FIELD not in event

    def test_handles_non_json_values_via_default_str(self) -> None:
        import datetime
        ts = datetime.datetime(2026, 4, 22, 12, 0, 0)
        signed = sign_event({"when": ts}, KEY_A)
        # The sig covers str(ts); verifying with the same object round-trips.
        assert verify_event(signed, KEY_A) is True


class TestLoadKeyFromEnv:
    def test_returns_none_when_missing(self) -> None:
        with patch.dict(os.environ, {ENV_VAR: ""}, clear=False):
            assert load_key_from_env() is None

    def test_returns_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_VAR, raising=False)
        assert load_key_from_env() is None

    def test_decodes_valid_base64(self) -> None:
        raw = b"\x00" * 32
        encoded = base64.b64encode(raw).decode()
        with patch.dict(os.environ, {ENV_VAR: encoded}):
            key = load_key_from_env()
        assert key == raw

    def test_rejects_invalid_base64(self) -> None:
        with patch.dict(os.environ, {ENV_VAR: "not base64!!!"}):
            assert load_key_from_env() is None

    def test_rejects_short_key(self) -> None:
        # 8 bytes is below the 16-byte floor — too short.
        encoded = base64.b64encode(b"shortkey").decode()
        with patch.dict(os.environ, {ENV_VAR: encoded}):
            assert load_key_from_env() is None

    def test_accepts_32_byte_key(self) -> None:
        raw = os.urandom(32)
        encoded = base64.b64encode(raw).decode()
        with patch.dict(os.environ, {ENV_VAR: encoded}):
            assert load_key_from_env() == raw


class TestSignatureFieldExclusion:
    """Signing and verification must both exclude _sig from the canonical
    form. Otherwise the signature would cover itself (chicken-and-egg)
    and verify would always fail."""

    def test_signing_twice_does_not_chain(self) -> None:
        e1 = sign_event({"x": 1}, KEY_A)
        # Sign again with _sig present — should produce same signature.
        e2 = sign_event(e1, KEY_A)
        assert e2[SIGNATURE_FIELD] == e1[SIGNATURE_FIELD]
