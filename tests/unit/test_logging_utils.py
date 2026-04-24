from __future__ import annotations

import time

import pytest

from tests.internal_asyncio_impl_refs import WarningRateLimiter


def test_warning_rate_limiter_rejects_non_positive_interval() -> None:
    with pytest.raises(ValueError, match="interval_seconds must be > 0"):
        WarningRateLimiter(interval_seconds=0)


def test_warning_rate_limiter_suppresses_repeated_warnings_within_interval() -> None:
    limiter = WarningRateLimiter(interval_seconds=0.05)

    assert limiter.should_log("x") is True
    assert limiter.should_log("x") is False
    time.sleep(0.06)
    assert limiter.should_log("x") is True


def test_warning_rate_limiter_rejects_non_positive_max_keys() -> None:
    with pytest.raises(ValueError, match="max_keys must be >= 1"):
        WarningRateLimiter(interval_seconds=1.0, max_keys=0)


def test_warning_rate_limiter_evicts_oldest_key_when_max_keys_reached() -> None:
    limiter = WarningRateLimiter(interval_seconds=60.0, max_keys=3)

    assert limiter.should_log("a") is True
    assert limiter.should_log("b") is True
    assert limiter.should_log("c") is True
    # Dict is now full (3 entries: a, b, c).  Adding "d" should evict "a" (FIFO).
    assert limiter.should_log("d") is True
    assert len(limiter._last_logged_at) == 3
    assert "a" not in limiter._last_logged_at
    assert "b" in limiter._last_logged_at
    assert "c" in limiter._last_logged_at
    assert "d" in limiter._last_logged_at


def test_warning_rate_limiter_does_not_evict_on_existing_key_update() -> None:
    limiter = WarningRateLimiter(interval_seconds=0.01, max_keys=2)

    assert limiter.should_log("a") is True
    assert limiter.should_log("b") is True
    # Both slots are occupied. Updating an existing key ("a") after interval
    # must NOT evict anything — the dict stays at 2 entries.
    time.sleep(0.02)
    assert limiter.should_log("a") is True
    assert len(limiter._last_logged_at) == 2
    assert "a" in limiter._last_logged_at
    assert "b" in limiter._last_logged_at
