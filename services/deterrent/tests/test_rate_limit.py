"""Tests for shared/rate_limit.py — Redis-backed fixed-window counter.

Lives under deterrent tests because any service importing the shared
module needs the same coverage; running from here exercises the exact
import path (``from rate_limit import RateLimiter``)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from rate_limit import RateLimiter


@pytest.fixture
def fake_redis() -> MagicMock:
    """A MagicMock that behaves enough like a redis client for the limiter."""
    client = MagicMock()
    client._counter = 0

    def _incr(_key: str) -> int:
        client._counter += 1
        return client._counter

    client.incr.side_effect = _incr
    client.expire.return_value = True
    client.ttl.return_value = 60
    return client


def test_under_capacity_allows(fake_redis: MagicMock) -> None:
    limiter = RateLimiter(fake_redis)
    allowed, retry = limiter.check("user:1", "test-fire", capacity=5, window_seconds=60)
    assert allowed is True
    assert retry == 0


def test_at_capacity_allows_last(fake_redis: MagicMock) -> None:
    limiter = RateLimiter(fake_redis)
    for _ in range(5):
        allowed, _ = limiter.check("user:1", "s", capacity=5, window_seconds=60)
        assert allowed is True


def test_over_capacity_denies(fake_redis: MagicMock) -> None:
    limiter = RateLimiter(fake_redis)
    for _ in range(5):
        limiter.check("user:1", "s", capacity=5, window_seconds=60)
    allowed, retry = limiter.check("user:1", "s", capacity=5, window_seconds=60)
    assert allowed is False
    assert retry > 0


def test_expire_set_on_first_hit(fake_redis: MagicMock) -> None:
    limiter = RateLimiter(fake_redis)
    limiter.check("user:1", "s", capacity=5, window_seconds=90)
    # First call of the window should set the TTL
    assert fake_redis.expire.called
    # subsequent calls should NOT re-arm the TTL (avoids extending the window)
    fake_redis.expire.reset_mock()
    limiter.check("user:1", "s", capacity=5, window_seconds=90)
    fake_redis.expire.assert_not_called()


def test_fail_open_on_redis_error(fake_redis: MagicMock) -> None:
    import redis as redis_lib
    fake_redis.incr.side_effect = redis_lib.RedisError("boom")
    limiter = RateLimiter(fake_redis)
    allowed, _ = limiter.check("user:1", "s", capacity=5, window_seconds=60)
    # Better to let legitimate traffic through during a Redis outage than
    # to hard-block the whole UI.
    assert allowed is True


def test_zero_capacity_always_allows(fake_redis: MagicMock) -> None:
    """Degenerate config — capacity=0 or window=0 disables the limit."""
    limiter = RateLimiter(fake_redis)
    allowed, _ = limiter.check("user:1", "s", capacity=0, window_seconds=60)
    assert allowed is True
    allowed, _ = limiter.check("user:1", "s", capacity=10, window_seconds=0)
    assert allowed is True


def test_retry_after_uses_ttl(fake_redis: MagicMock) -> None:
    """When denied, retry_after should be drawn from the key's TTL, not a constant."""
    fake_redis.ttl.return_value = 42
    limiter = RateLimiter(fake_redis)
    # Push over capacity
    for _ in range(6):
        limiter.check("user:1", "s", capacity=5, window_seconds=60)
    _, retry = limiter.check("user:1", "s", capacity=5, window_seconds=60)
    assert retry == 42


def test_retry_after_falls_back_to_window_when_ttl_bogus(fake_redis: MagicMock) -> None:
    fake_redis.ttl.return_value = -1  # no TTL set somehow
    limiter = RateLimiter(fake_redis)
    for _ in range(6):
        limiter.check("user:1", "s", capacity=5, window_seconds=60)
    _, retry = limiter.check("user:1", "s", capacity=5, window_seconds=60)
    assert retry == 60


def test_separate_principals_have_separate_counters() -> None:
    # Fresh MagicMock pair; MagicMocks share state otherwise.
    client = MagicMock()
    counts: dict[str, int] = {}

    def _incr(key: str) -> int:
        counts[key] = counts.get(key, 0) + 1
        return counts[key]

    client.incr.side_effect = _incr
    client.expire.return_value = True
    client.ttl.return_value = 60
    limiter = RateLimiter(client)

    for _ in range(5):
        limiter.check("user:1", "s", capacity=5, window_seconds=60)
    # user:2 still starts fresh
    allowed, _ = limiter.check("user:2", "s", capacity=5, window_seconds=60)
    assert allowed is True
