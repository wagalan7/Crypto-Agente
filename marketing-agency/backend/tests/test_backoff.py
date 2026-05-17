"""Unit tests for publish retry backoff schedule."""
from datetime import timedelta

from services.scheduler import _next_retry_delay, BACKOFF_MINUTES, MAX_PUBLISH_ATTEMPTS


def test_backoff_increases_monotonically():
    delays = [_next_retry_delay(i).total_seconds() for i in range(len(BACKOFF_MINUTES))]
    assert delays == sorted(delays)


def test_first_attempt_short():
    assert _next_retry_delay(0) == timedelta(minutes=BACKOFF_MINUTES[0])


def test_caps_at_last_entry():
    assert _next_retry_delay(99) == timedelta(minutes=BACKOFF_MINUTES[-1])


def test_max_attempts_sane():
    assert 3 <= MAX_PUBLISH_ATTEMPTS <= 10
