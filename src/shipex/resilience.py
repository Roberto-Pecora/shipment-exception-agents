"""Retry with backoff and a circuit breaker for outbound calls (LLM, carrier-gateway).

Clocks and sleeps are injectable so the behaviour is tested without real waits.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from typing import Literal, TypeVar

T = TypeVar("T")

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def backoff_delay(
    attempt: int, base_delay: float, max_delay: float, retry_after: float | None = None
) -> float:
    """Honour Retry-After when given, else jittered exponential backoff."""
    if retry_after is not None:
        return min(max(retry_after, 0.0), max_delay)
    capped = min(max_delay, base_delay * 2 ** (attempt - 1))
    return float(capped * (0.5 + random.random() / 2))


def call_with_retry(
    fn: Callable[[], T],
    *,
    is_retryable: Callable[[Exception], bool],
    retry_after: Callable[[Exception], float | None] = lambda exc: None,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == max_attempts or not is_retryable(exc):
                raise
            sleep(backoff_delay(attempt, base_delay, max_delay, retry_after(exc)))
    raise AssertionError("unreachable")


class CircuitOpenError(Exception):
    """Raised without calling the dependency while the breaker is open."""


class CircuitBreaker:
    """Closed -> open after N consecutive failures; half-open probe after a cool-down."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        reset_timeout_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> Literal["closed", "open", "half_open"]:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self._reset_timeout:
            return "half_open"
        return "open"

    def call(self, fn: Callable[[], T], is_failure: Callable[[Exception], bool]) -> T:
        with self._lock:
            if self.state == "open":
                raise CircuitOpenError(f"{self.name} circuit is open")
        try:
            result = fn()
        except Exception as exc:
            if is_failure(exc):
                with self._lock:
                    self._failures += 1
                    if self._failures >= self._threshold or self._opened_at is not None:
                        self._opened_at = self._clock()
            raise
        with self._lock:
            self._failures = 0
            self._opened_at = None
        return result
