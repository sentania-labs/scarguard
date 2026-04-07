"""Unit tests for `main._parse_auth_enabled`.

Regression for PR #94 codex review — the original code used
``bool(auth_cfg.get("enabled", True))`` which mishandled quoted YAML
values (``bool("false") is True``), silently keeping auth enabled when
the operator intended to disable it.
"""

from __future__ import annotations

import pytest

from main import _parse_auth_enabled


@pytest.mark.parametrize(
    "value",
    [False, "false", "False", "FALSE", " false ", "0", "no", "off", ""],
)
def test_false_values(value):
    assert _parse_auth_enabled(value) is False


@pytest.mark.parametrize(
    "value",
    [True, "true", "True", "1", "yes", "on", "enabled"],
)
def test_true_values(value):
    assert _parse_auth_enabled(value) is True


def test_missing_defaults_true_via_caller():
    # Caller supplies the default; the helper itself just coerces.
    # Simulate the "missing key" path by passing the caller's default.
    assert _parse_auth_enabled(True) is True
