import time

from aitos.risk.circuit_breaker import CircuitBreaker
from aitos.risk.models import CircuitBreakerState


def test_starts_closed_and_allows_trading():
    cb = CircuitBreaker()
    assert cb.state == CircuitBreakerState.CLOSED
    assert cb.is_trading_allowed() is True


def test_trip_opens_and_blocks_trading():
    cb = CircuitBreaker()
    cb.trip("max drawdown breached")
    assert cb.state == CircuitBreakerState.OPEN
    assert cb.is_trading_allowed() is False
    assert cb.history[-1].reason == "max drawdown breached"


def test_cooldown_not_elapsed_blocks_half_open_transition():
    cb = CircuitBreaker(cooldown_seconds=100.0)
    cb.trip("test")
    assert cb.cooldown_elapsed() is False
    assert cb.attempt_half_open() is False
    assert cb.state == CircuitBreakerState.OPEN


def test_cooldown_elapsed_allows_half_open_transition():
    cb = CircuitBreaker(cooldown_seconds=0.05)
    cb.trip("test")
    time.sleep(0.06)
    assert cb.cooldown_elapsed() is True
    assert cb.attempt_half_open() is True
    assert cb.state == CircuitBreakerState.HALF_OPEN
    assert cb.is_trading_allowed() is True  # probing allowed


def test_half_open_success_closes_breaker():
    cb = CircuitBreaker(cooldown_seconds=0.01)
    cb.trip("test")
    time.sleep(0.02)
    cb.attempt_half_open()
    cb.record_probe_result(success=True)
    assert cb.state == CircuitBreakerState.CLOSED


def test_half_open_failure_reopens_breaker():
    cb = CircuitBreaker(cooldown_seconds=0.01)
    cb.trip("test")
    time.sleep(0.02)
    cb.attempt_half_open()
    cb.record_probe_result(success=False, reason="probe trade also lost")
    assert cb.state == CircuitBreakerState.OPEN
    assert cb.history[-1].reason == "probe trade also lost"


def test_record_probe_result_ignored_when_not_half_open():
    cb = CircuitBreaker()
    cb.record_probe_result(success=True)
    assert cb.state == CircuitBreakerState.CLOSED  # no-op, nothing to resolve


def test_manual_reset_forces_closed():
    cb = CircuitBreaker()
    cb.trip("test")
    cb.reset()
    assert cb.state == CircuitBreakerState.CLOSED
    assert cb.is_trading_allowed() is True
