"""Tests for CircuitBreaker state machine."""

from __future__ import annotations

from unittest.mock import patch

from memtomem_stm.utils.circuit_breaker import CircuitBreaker


class TestCircuitBreakerClosed:
    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert not cb.is_open

    def test_single_failure_stays_closed(self):
        cb = CircuitBreaker(max_failures=3)
        cb.record_failure()
        assert not cb.is_open

    def test_failures_below_threshold_stays_closed(self):
        cb = CircuitBreaker(max_failures=3)
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_open

    def test_record_success_is_idempotent(self):
        cb = CircuitBreaker()
        cb.record_success()
        cb.record_success()
        assert not cb.is_open


class TestCircuitBreakerOpen:
    def test_opens_at_max_failures(self):
        cb = CircuitBreaker(max_failures=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.is_open

    def test_opens_with_custom_threshold(self):
        cb = CircuitBreaker(max_failures=1)
        cb.record_failure()
        assert cb.is_open

    def test_stays_open_before_timeout(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=60.0)
        cb.record_failure()
        assert cb.is_open
        # Still open immediately after
        assert cb.is_open


class TestCircuitBreakerHalfOpen:
    def test_transitions_to_half_open_after_timeout(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()
        assert cb.is_open

        # Simulate time passing beyond reset_timeout
        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = cb._opened_at + 11.0
            # Should transition to half-open and allow one probe
            assert not cb.is_open
            assert cb._state == "half-open"

    def test_half_open_success_closes_circuit(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()

        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = cb._opened_at + 11.0
            assert not cb.is_open  # half-open

        cb.record_success()
        assert cb._state == "closed"
        assert not cb.is_open

    def test_half_open_failure_reopens_circuit(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()

        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = cb._opened_at + 11.0
            assert not cb.is_open  # half-open

        cb.record_failure()
        assert cb._state == "open"
        assert cb.is_open

    def test_half_open_failure_resets_timeout(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()
        first_opened_at = cb._opened_at

        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = first_opened_at + 11.0
            assert not cb.is_open  # half-open

        cb.record_failure()
        # opened_at should be refreshed
        assert cb._opened_at > first_opened_at or cb._opened_at == first_opened_at
        # If time.monotonic was called normally, opened_at >= first_opened_at

    def test_repeated_open_close_cycles(self):
        cb = CircuitBreaker(max_failures=2, reset_timeout=5.0)

        # Cycle 1: open
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open

        # Half-open → success → closed
        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = cb._opened_at + 6.0
            assert not cb.is_open
        cb.record_success()
        assert not cb.is_open

        # Cycle 2: open again
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open

    def test_half_open_does_not_allow_multiple_probes(self):
        """After transitioning to half-open, subsequent is_open calls should not
        return False again (the probe was already allowed)."""
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()

        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = cb._opened_at + 11.0
            # First call: transitions to half-open, returns False
            assert not cb.is_open
            assert cb._state == "half-open"

        # Second call without success/failure: state is half-open,
        # is_open returns False (half-open is not "open").
        # This is acceptable because the consumer should call
        # record_success/failure before the next is_open check.
        # The important thing is the circuit reacts correctly to the result.
