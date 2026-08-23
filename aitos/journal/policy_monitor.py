"""Event-driven active policy performance monitoring."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Mapping, Optional


@dataclass(frozen=True)
class PolicyHealth:
    version: str
    observations: int
    avg_r: float
    win_rate: float
    baseline_avg_r: float
    degradation: float
    rollback_recommended: bool
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


class PolicyMonitor:
    def __init__(
        self,
        version: str,
        *,
        baseline_avg_r: float,
        window_size: int = 100,
        min_observations: int = 30,
        max_degradation: float = 0.20,
        min_avg_r: float = 0.0,
    ) -> None:
        self.version = version
        self.baseline_avg_r = float(baseline_avg_r)
        self.window_size = max(1, int(window_size))
        self.min_observations = max(1, int(min_observations))
        self.max_degradation = float(max_degradation)
        self.min_avg_r = float(min_avg_r)
        self._outcomes: Deque[float] = deque(maxlen=self.window_size)

    def record_outcome(self, outcome: Mapping[str, Any]) -> PolicyHealth:
        value = outcome.get("r_multiple")
        if isinstance(value, (int, float)):
            self._outcomes.append(float(value))
        return self.health()

    def health(self) -> PolicyHealth:
        rs = list(self._outcomes)
        avg = sum(rs) / len(rs) if rs else 0.0
        win_rate = sum(1 for r in rs if r > 0) / len(rs) if rs else 0.0
        degradation = (
            ((self.baseline_avg_r - avg) / abs(self.baseline_avg_r))
            if self.baseline_avg_r > 0
            else 0.0
        )
        bad = len(rs) >= self.min_observations and (
            avg < self.min_avg_r or degradation >= self.max_degradation
        )
        reason = (
            "rollback_recommended"
            if bad
            else (
                "insufficient_observations"
                if len(rs) < self.min_observations
                else "policy_within_guardrails"
            )
        )
        return PolicyHealth(
            self.version,
            len(rs),
            avg,
            win_rate,
            self.baseline_avg_r,
            degradation,
            bad,
            reason,
        )

    def reset(self, version: str, baseline_avg_r: Optional[float] = None) -> None:
        self.version = version
        if baseline_avg_r is not None:
            self.baseline_avg_r = float(baseline_avg_r)
        self._outcomes.clear()


def evaluate_policy_health(
    version: str,
    outcomes: list[Mapping[str, Any]],
    *,
    baseline_avg_r: float,
    min_observations: int = 30,
    max_degradation: float = 0.20,
    min_avg_r: float = 0.0,
) -> PolicyHealth:
    """Compatibility helper for batch evaluation and existing callers."""
    monitor = PolicyMonitor(
        version,
        baseline_avg_r=baseline_avg_r,
        min_observations=min_observations,
        max_degradation=max_degradation,
        min_avg_r=min_avg_r,
        window_size=max(1, len(outcomes)),
    )
    for outcome in outcomes:
        monitor.record_outcome(outcome)
    return monitor.health()
