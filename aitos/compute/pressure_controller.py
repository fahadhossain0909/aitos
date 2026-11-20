"""Hysteretic, advisory pressure control for optional compute workloads.

The controller never pauses market-data integrity, position monitoring, risk,
or execution responsibilities. Callers may use its decision only to defer
optional/background work; it does not resize trading risk or mutate strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import os
from pathlib import Path
from time import monotonic
from typing import Callable

from aitos.compute.resource_governor import (
    PressureLevel,
    ResourceSnapshot,
    classify_pressure,
)


@dataclass(frozen=True)
class ProcessPressureSample:
    observed_at: float
    cpu_utilization_pct: float
    memory_utilization_pct: float
    queue_lag_seconds: float


def _process_cpu_seconds() -> float:
    usage = os.times()
    return float(usage.user + usage.system)


def _process_rss_mb() -> float | None:
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        return None
    return None


class ProcessPressureSampler:
    """Sample process CPU/RSS against the startup cgroup-aware resource limit.

    The first sample establishes a CPU baseline and reports zero CPU pressure;
    subsequent samples use monotonic wall time and process CPU time. Queue lag
    is supplied by the owning event/worker queue, not guessed here.
    """

    def __init__(
        self,
        resources: ResourceSnapshot,
        *,
        clock: Callable[[], float] = monotonic,
        cpu_time_reader: Callable[[], float] = _process_cpu_seconds,
        memory_reader: Callable[[], float | None] = _process_rss_mb,
    ) -> None:
        if (
            not callable(clock)
            or not callable(cpu_time_reader)
            or not callable(memory_reader)
        ):
            raise TypeError("pressure sampler readers must be callable")
        self.resources = resources
        self._clock = clock
        self._cpu_time_reader = cpu_time_reader
        self._memory_reader = memory_reader
        self._previous_wall: float | None = None
        self._previous_cpu: float | None = None

    def sample(self, *, queue_lag_seconds: float) -> ProcessPressureSample:
        now = float(self._clock())
        cpu_seconds = float(self._cpu_time_reader())
        if (
            not isfinite(now)
            or now < 0
            or not isfinite(cpu_seconds)
            or cpu_seconds < 0
            or not isfinite(queue_lag_seconds)
            or queue_lag_seconds < 0
        ):
            raise ValueError("pressure sampler inputs must be finite and non-negative")

        cpu_pct = 0.0
        if self._previous_wall is not None and self._previous_cpu is not None:
            elapsed = now - self._previous_wall
            cpu_delta = cpu_seconds - self._previous_cpu
            if elapsed <= 0 or cpu_delta < 0:
                raise ValueError("pressure sampler clocks must be monotonic")
            cpu_pct = min(
                100.0,
                max(0.0, cpu_delta / (elapsed * self.resources.effective_cpus) * 100),
            )
        self._previous_wall = now
        self._previous_cpu = cpu_seconds

        rss_mb = self._memory_reader()
        if rss_mb is None:
            used_mb = max(
                0,
                self.resources.memory_limit_mb - self.resources.memory_available_mb,
            )
        else:
            used_mb = float(rss_mb)
            if not isfinite(used_mb) or used_mb < 0:
                raise ValueError("process RSS must be finite and non-negative")
        memory_pct = (
            min(100.0, used_mb / self.resources.memory_limit_mb * 100.0)
            if self.resources.memory_limit_mb > 0
            else 0.0
        )
        return ProcessPressureSample(
            observed_at=now,
            cpu_utilization_pct=cpu_pct,
            memory_utilization_pct=memory_pct,
            queue_lag_seconds=float(queue_lag_seconds),
        )


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
        if any(not isfinite(value) or value < 0 for value in values):
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
                else (
                    self.policy.high_hold_seconds
                    if observed == PressureLevel.HIGH
                    else self.policy.warning_hold_seconds
                )
            )
            if self._candidate != observed:
                self._candidate = observed
                self._candidate_since = now_seconds
            elapsed = now_seconds - (
                self._candidate_since
                if self._candidate_since is not None
                else now_seconds
            )
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
            elapsed = now_seconds - (
                self._candidate_since
                if self._candidate_since is not None
                else now_seconds
            )
            cooldown_ok = (
                self._last_transition is None
                or now_seconds - self._last_transition
                >= self.policy.minimum_transition_interval_seconds
            )
            if elapsed >= self.policy.recovery_hold_seconds and cooldown_ok:
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
