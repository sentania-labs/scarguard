"""Unit tests for `main._parse_auth_enabled`.

Contract (SG-29 / FDY-0608): only an explicit boolean ``False`` disables
authentication.  String values like ``"false"``, numeric ``0``, empty string,
etc. are treated as enabled (safe default).  In TLS mode the middleware
rejects any attempt to disable auth, but the helper itself is pure and does
not inspect TLS status - that is handled by the caller.
"""

from __future__ import annotations

from main import _parse_auth_enabled


def test_boolean_false_disables():
    """Only explicit boolean False disables auth."""
    assert _parse_auth_enabled(False) is False


def test_boolean_true_enables():
    """Explicit boolean True enables auth."""
    assert _parse_auth_enabled(True) is True


def test_string_false_enables():
    """YAML string 'false' is treated as enabled (ambiguous setting)."""
    assert _parse_auth_enabled("false") is True
    assert _parse_auth_enabled("False") is True
    assert _parse_auth_enabled("FALSE") is True
    assert _parse_auth_enabled(" false ") is True


def test_numeric_zero_enables():
    """Numeric 0 is treated as enabled (ambiguous setting)."""
    assert _parse_auth_enabled(0) is True
    assert _parse_auth_enabled(1) is True
    assert _parse_auth_enabled(0.0) is True
    assert _parse_auth_enabled(1.0) is True


def test_word_values_enables():
    """Word values like 'no', 'off' are treated as enabled."""
    assert _parse_auth_enabled("no") is True
    assert _parse_auth_enabled("off") is True
    assert _parse_auth_enabled("yes") is True
    assert _parse_auth_enabled("on") is True
    assert _parse_auth_enabled("enabled") is True


def test_empty_string_enables():
    """Empty string is treated as enabled."""
    assert _parse_auth_enabled("") is True


def test_none_enables():
    """None is treated as enabled."""
    assert _parse_auth_enabled(None) is True


def test_missing_defaults_true():
    """The caller's default (True) means enabled."""
    assert _parse_auth_enabled(True) is True
