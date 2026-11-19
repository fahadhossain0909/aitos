"""Hysteretic, advisory pressure control for optional compute workloads.

The controller never pauses market-data integrity, position monitoring, risk,
or execution responsibilities. Callers may use its decision only to defer
optional/background work; it does not resize trading risk or mutate strategy.
"""
from __future__ import annotations

from dataclasses import dataclass

from aitos.compute.resource_governor import PressureLevel, classify_pressure


@dataclass(frozen=True)
class PressurePolicy:
    warning_hold_seconds: float = 5.0
    high_hold_seconds: float = 3.0
    recovery_hold_seconds: float = 30.0
    minimum_transition_interval_seconds: float = 10.0

    def __post_init__(self) -> None:
        values = (
            self.warning_hold_seconds,
            self.high_hold_seconds,
            self.recovery_hold_seconds,
            self.minimum_transition_interval_seconds,
        )
        if any(value < 0 for value in values):
            raise ValueError("pressure policy durations must be non-negative")


@dataclass(frozen=True)
class PressureDecision:
    level: PressureLevel
    changed: bool
    defer_optional_work: bool
    allow_background_work: bool
    reason: str


class ResourcePressureController:
    """Stateful pressure gate with delayed escalation and slower recovery."""

    def __init__(self, policy: PressurePolicy | None = None) -> None:
        self.policy = policy or PressurePolicy()
        self._level = PressureLevel.NORMAL
        self._candidate: PressureLevel | None = None
        self._candidate_since: float | None = None
        self._last_transition: float | None = None
        self._last_observed_at: float | None = None

    @property
    def level(self) -> PressureLevel:
        return self._level

    def update(
        self,
        *,
        now_seconds: float,
        cpu_utilization_pct: float,
        memory_utilization_pct: float,
        queue_lag_seconds: float,
    ) -> PressureDecision:
        """Observe pressure; CRITICAL is immediate, recovery is deliberately slow."""
        from math import isfinite

        if not isfinite(now_seconds) or now_seconds < 0:
            raise ValueError("now_seconds must be finite and non-negative")
        if self._last_observed_at is not None and now_seconds < self._last_observed_at:
            raise ValueError("now_seconds must be monotonic")
        self._last_observed_at = now_seconds
        observed = classify_pressure(
            cpu_utilization_pct=cpu_utilization_pct,
            memory_utilization_pct=memory_utilization_pct,
            queue_lag_seconds=queue_lag_seconds,
        )
        changed = False
        reason = "pressure_unchanged"

        if observed > self._level:
            required = (
                0.0
                if observed == PressureLevel.CRITICAL
                else self.policy.high_hold_seconds
                if observed == PressureLevel.HIGH
                else self.policy.warning_hold_seconds
            )
            if self._candidate != observed:
                self._candidate = observed
                self._candidate_since = now_seconds
            elapsed = now_seconds - (self._candidate_since or now_seconds)
            cooldown_ok = (
                self._last_transition is None
                or now_seconds - self._last_transition
                >= self.policy.minimum_transition_interval_seconds
                or observed == PressureLevel.CRITICAL
            )
            if elapsed >= required and cooldown_ok:
                self._level = observed
                self._last_transition = now_seconds
                self._candidate = None
                self._candidate_since = None
                changed = True
                reason = "pressure_escalated"
            else:
                reason = "escalation_hysteresis"
        elif observed < self._level:
            if self._candidate != observed:
                self._candidate = observed
                self._candidate_since = now_seconds
            elapsed = now_seconds - (self._candidate_since or now_seconds)
            if elapsed >= self.policy.recovery_hold_seconds:
                self._level = observed
                self._last_transition = now_seconds
                self._candidate = None
                self._candidate_since = None
                changed = True
                reason = "pressure_recovered"
            else:
                reason = "recovery_hysteresis"
        else:
            self._candidate = None
            self._candidate_since = None

        return PressureDecision(
            level=self._level,
            changed=changed,
            defer_optional_work=self._level >= PressureLevel.HIGH,
            allow_background_work=self._level == PressureLevel.NORMAL,
            reason=reason,
        )
