"""Unified circuit breaker for STM subsystems."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Three-state circuit breaker: closed → open → half-open.

    - **closed**: all calls pass through.
    - **open**: all calls blocked; transitions to half-open after ``reset_timeout``.
    - **half-open**: one probe call allowed; success closes, failure re-opens.
    """

    def __init__(
        self,
        max_failures: int = 3,
        reset_timeout: float = 60.0,
        name: str = "",
    ) -> None:
        self._max_failures = max_failures
        self._reset_timeout = reset_timeout
        self._name = name
        self._failures = 0
        self._state = "closed"
        self._opened_at = 0.0

    @property
    def is_open(self) -> bool:
        if self._state == "closed":
            return False
        if self._state == "open" and time.monotonic() - self._opened_at >= self._reset_timeout:
            self._state = "half-open"
            return False  # allow one probe
        return self._state == "open"

    def record_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    def record_failure(self) -> None:
        self._failures += 1
        if self._state == "half-open" or (
            self._failures >= self._max_failures and self._state == "closed"
        ):
            self._state = "open"
            self._opened_at = time.monotonic()
            logger.warning(
                "CircuitBreaker[%s] opened after %d failures", self._name, self._failures
            )

    # Aliases for backward compatibility
    success = record_success
    failure = record_failure
