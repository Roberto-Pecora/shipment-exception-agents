from __future__ import annotations

import pytest

from shipex.resilience import CircuitBreaker, CircuitOpenError, backoff_delay, call_with_retry


def test_backoff_honours_retry_after_and_caps() -> None:
    assert backoff_delay(1, 0.5, 8.0, retry_after=3.0) == 3.0
    assert backoff_delay(1, 0.5, 8.0, retry_after=60.0) == 8.0
    assert 2.0 <= backoff_delay(4, 0.5, 8.0) <= 4.0


def test_call_with_retry_stops_on_non_retryable() -> None:
    calls = {"n": 0}

    def boom() -> int:
        calls["n"] += 1
        raise ValueError("bad request")

    with pytest.raises(ValueError):
        call_with_retry(boom, is_retryable=lambda e: False, sleep=lambda s: None)
    assert calls["n"] == 1


def test_breaker_half_opens_after_cooldown() -> None:
    now = {"t": 0.0}
    breaker = CircuitBreaker("dep", failure_threshold=1, reset_timeout_s=10, clock=lambda: now["t"])

    def fail() -> int:
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        breaker.call(fail, is_failure=lambda e: True)
    assert breaker.state == "open"
    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: 1, is_failure=lambda e: True)

    now["t"] = 11.0
    assert breaker.state == "half_open"
    assert breaker.call(lambda: 1, is_failure=lambda e: True) == 1
    assert breaker.state == "closed"
