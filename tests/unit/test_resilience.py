import pytest

from ftcommon.resilience import CLOSED, HALF_OPEN, OPEN, CircuitBreaker, RetryableError, retry_call


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


# ------------------------------------------------------------ circuit breaker
def test_breaker_opens_after_threshold_and_short_circuits():
    cb = CircuitBreaker("dep", failure_threshold=3, reset_timeout=5, clock=FakeClock())
    for _ in range(2):
        assert cb.allow_request()
        cb.record_failure()
    assert cb.state == CLOSED
    cb.record_failure()
    assert cb.state == OPEN
    assert not cb.allow_request()


def test_breaker_half_open_allows_single_trial_then_closes_on_success():
    clock = FakeClock()
    cb = CircuitBreaker("dep", failure_threshold=1, reset_timeout=5, clock=clock)
    cb.record_failure()
    assert not cb.allow_request()
    clock.now = 5.1
    assert cb.state == HALF_OPEN
    assert cb.allow_request()          # the trial request
    assert not cb.allow_request()      # concurrent requests still blocked
    cb.record_success()
    assert cb.state == CLOSED
    assert cb.allow_request()


def test_breaker_reopens_when_trial_fails():
    clock = FakeClock()
    cb = CircuitBreaker("dep", failure_threshold=1, reset_timeout=5, clock=clock)
    cb.record_failure()
    clock.now = 6
    assert cb.allow_request()
    cb.record_failure()
    assert cb.state == OPEN
    clock.now = 7
    assert not cb.allow_request()


def test_success_resets_failure_count():
    cb = CircuitBreaker("dep", failure_threshold=3, reset_timeout=5, clock=FakeClock())
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CLOSED


# ------------------------------------------------------------------ retries
def test_retry_succeeds_after_transient_failures():
    calls = {"n": 0}
    sleeps = []

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RetryableError("transient")
        return "ok"

    assert retry_call(flaky, attempts=3, base_delay=0.1, sleep=sleeps.append) == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2
    assert all(0 <= s <= 0.2 for s in sleeps)  # exponential backoff with full jitter


def test_retry_is_bounded():
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise RetryableError("down")

    with pytest.raises(RetryableError):
        retry_call(always_fails, attempts=3, base_delay=0.01, sleep=lambda s: None)
    assert calls["n"] == 3


def test_non_retryable_errors_are_not_retried():
    calls = {"n": 0}

    def bad_request():
        calls["n"] += 1
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        retry_call(bad_request, attempts=5, base_delay=0.01, sleep=lambda s: None)
    assert calls["n"] == 1
