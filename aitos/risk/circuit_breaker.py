"""Circuit breaker state machine — spec section 23.3.

    CLOSED   → Normal operation
      ↓ (trigger condition)
    OPEN     → Trading paused, positions managed but no new entries
      ↓ (cooldown + manual check)
    HALF_OPEN → Test with small position
      ↓ (success)
    CLOSED

A failed probe while HALF_OPEN sends it back to OPEN with a fresh cooldown.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional

from aitos.risk.models import CircuitBreakerState


@dataclass
class CircuitBreakerEvent:
    state: CircuitBreakerState
    reason: str
    at_monotonic: float


class CircuitBreaker:
    def __init__(self, cooldown_seconds: float = 300.0) -> None:
        self._state = CircuitBreakerState.CLOSED
        self._cooldown_seconds = cooldown_seconds
        self._tripped_at: Optional[float] = None
        self._history: List[CircuitBreakerEvent] = []

    @property
    def state(self) -> CircuitBreakerState:
        return self._state

    @property
    def history(self) -> List[CircuitBreakerEvent]:
        return list(self._history)

    def is_trading_allowed(self) -> bool:
        """New entries are only allowed when CLOSED. HALF_OPEN allows probing
        (callers should size any HALF_OPEN trade down themselves); OPEN allows
        neither."""
        return self._state in (
            CircuitBreakerState.CLOSED,
            CircuitBreakerState.HALF_OPEN,
        )

    def trip(self, reason: str) -> None:
        """Force to OPEN from any state (e.g. drawdown breach, flash crash)."""
        self._state = CircuitBreakerState.OPEN
        self._tripped_at = time.monotonic()
        self._history.append(CircuitBreakerEvent(self._state, reason, self._tripped_at))

    def cooldown_elapsed(self) -> bool:
        if self._state != CircuitBreakerState.OPEN or self._tripped_at is None:
            return False
        return (time.monotonic() - self._tripped_at) >= self._cooldown_seconds

    def attempt_half_open(self) -> bool:
        """Transition OPEN -> HALF_OPEN once the cooldown has elapsed. Returns
        whether the transition happened."""
        if self._state == CircuitBreakerState.OPEN and self.cooldown_elapsed():
            self._state = CircuitBreakerState.HALF_OPEN
            self._history.append(
                CircuitBreakerEvent(
                    self._state, "cooldown elapsed, probing", time.monotonic()
                )
            )
            return True
        return False

    def record_probe_result(self, success: bool, reason: str = "") -> None:
        """Only meaningful while HALF_OPEN: success -> CLOSED, failure -> OPEN
        (with cooldown restarted)."""
        if self._state != CircuitBreakerState.HALF_OPEN:
            return
        if success:
            self._state = CircuitBreakerState.CLOSED
            self._tripped_at = None
            self._history.append(
                CircuitBreakerEvent(
                    self._state, reason or "probe succeeded", time.monotonic()
                )
            )
        else:
            self._state = CircuitBreakerState.OPEN
            self._tripped_at = time.monotonic()
            self._history.append(
                CircuitBreakerEvent(
                    self._state, reason or "probe failed", self._tripped_at
                )
            )

    def reset(self) -> None:
        """Manual override back to CLOSED (e.g. human-approved recovery)."""
        self._state = CircuitBreakerState.CLOSED
        self._tripped_at = None
        self._history.append(
            CircuitBreakerEvent(self._state, "manual reset", time.monotonic())
        )
