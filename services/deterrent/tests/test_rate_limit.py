"""Tests for shared/rate_limit.py - Redis-backed fixed-window counter.

Lives under deterrent tests because any service importing the shared
module needs the same coverage; running from here exercises the exact
import path (``from rate_limit import RateLimiter``).

FDY-0563: the counter increment and its TTL are one atomic Lua script and the
limiter fails closed on every Redis error.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import redis as redis_lib
from rate_limit import INCR_WITH_TTL_SCRIPT, RateLimiter


@pytest.fixture
def fake_redis() -> MagicMock:
    """A MagicMock whose ``eval`` behaves like the limiter's atomic script."""
    client = MagicMock()
    client._counts = {}
    client._ttl = 60

    def _eval(script: str, numkeys: int, key: str, window: int) -> list[int]:
        assert script == INCR_WITH_TTL_SCRIPT and numkeys == 1
        client._counts[key] = client._counts.get(key, 0) + 1
        return [client._counts[key], client._ttl]

    client.eval.side_effect = _eval
    return client


def test_under_capacity_allows(fake_redis: MagicMock) -> None:
    allowed, retry = RateLimiter(fake_redis).check("user:1", "test-fire", capacity=5, window_seconds=60)
    assert (allowed, retry) == (True, 0)


def test_over_capacity_denies_with_ttl(fake_redis: MagicMock) -> None:
    limiter = RateLimiter(fake_redis)
    for _ in range(5):
        assert limiter.check("user:1", "s", capacity=5, window_seconds=60)[0] is True
    fake_redis._ttl = 42
    assert limiter.check("user:1", "s", capacity=5, window_seconds=60) == (False, 42)


def test_counter_and_ttl_are_one_atomic_call(fake_redis: MagicMock) -> None:
    RateLimiter(fake_redis).check("user:1", "s", capacity=5, window_seconds=90)
    fake_redis.eval.assert_called_once_with(INCR_WITH_TTL_SCRIPT, 1, "rl:v1:s:user:1", 90)
    fake_redis.incr.assert_not_called()
    fake_redis.expire.assert_not_called()
    assert "INCR" in INCR_WITH_TTL_SCRIPT and "EXPIRE" in INCR_WITH_TTL_SCRIPT


@pytest.mark.parametrize(
    "error",
    [redis_lib.ConnectionError("down"), redis_lib.ResponseError("NOPERM"), redis_lib.ResponseError("OOM")],
)
def test_fail_closed_on_redis_error(fake_redis: MagicMock, error: Exception) -> None:
    fake_redis.eval.side_effect = error
    assert RateLimiter(fake_redis).check("user:1", "s", capacity=5, window_seconds=60) == (False, 60)


def test_bogus_ttl_falls_back_to_window(fake_redis: MagicMock) -> None:
    limiter = RateLimiter(fake_redis)
    for _ in range(5):
        limiter.check("user:1", "s", capacity=5, window_seconds=60)
    fake_redis._ttl = -1
    assert limiter.check("user:1", "s", capacity=5, window_seconds=60) == (False, 60)


def test_zero_capacity_always_allows(fake_redis: MagicMock) -> None:
    assert RateLimiter(fake_redis).check("user:1", "s", capacity=0, window_seconds=60) == (True, 0)
    fake_redis.eval.assert_not_called()


def test_separate_principals_have_separate_counters(fake_redis: MagicMock) -> None:
    limiter = RateLimiter(fake_redis)
    for _ in range(5):
        limiter.check("user:1", "s", capacity=5, window_seconds=60)
    assert limiter.check("user:1", "s", capacity=5, window_seconds=60)[0] is False
    assert limiter.check("user:2", "s", capacity=5, window_seconds=60)[0] is True
