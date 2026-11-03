"""Safe candidate-policy generation for autonomous decision adaptation.

This module never mutates production policy. It converts historical performance
signals into a bounded candidate policy that can later be evaluated in shadow
mode and explicitly promoted by governance.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.journal.performance_evaluator import PerformanceReport


@dataclass(frozen=True)
class RegimePolicy:
    regime: str
    enabled: bool
    min_confidence: float
    trades: int
    win_rate: float
    average_r_multiple: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime,
            "enabled": self.enabled,
            "min_confidence": self.min_confidence,
            "trades": self.trades,
            "win_rate": self.win_rate,
            "average_r_multiple": self.average_r_multiple,
        }


@dataclass(frozen=True)
class PolicyCandidate:
    candidate_id: str
    base_policy_version: str
    min_trades: int
    min_confidence_floor: float
    max_confidence_ceiling: float
    regimes: Mapping[str, RegimePolicy] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "base_policy_version": self.base_policy_version,
            "min_trades": self.min_trades,
            "min_confidence_floor": self.min_confidence_floor,
            "max_confidence_ceiling": self.max_confidence_ceiling,
            "regimes": {key: value.to_dict() for key, value in self.regimes.items()},
        }


class AdaptivePolicyEngine(AITOSModule):
    """Generate bounded, reviewable policy candidates from historical results."""

    def __init__(
        self,
        *,
        min_trades: int = 20,
        base_min_confidence: float = 0.60,
        confidence_floor: float = 0.55,
        confidence_ceiling: float = 0.90,
        base_policy_version: str = "fusion-v1",
    ) -> None:
        if min_trades < 1:
            raise ValueError("min_trades must be positive")
        if (
            not 0.0
            <= confidence_floor
            <= base_min_confidence
            <= confidence_ceiling
            <= 1.0
        ):
            raise ValueError("confidence bounds must satisfy floor <= base <= ceiling")
        self.min_trades = min_trades
        self.base_min_confidence = base_min_confidence
        self.confidence_floor = confidence_floor
        self.confidence_ceiling = confidence_ceiling
        self.base_policy_version = base_policy_version
        self._initialized = False
        self._last_candidate: PolicyCandidate | None = None

    @property
    def module_id(self) -> str:
        return "adaptive-policy-engine"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        self._initialized = True

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        self._initialized = False

    async def emit_events(self):
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            module_id=self.module_id,
            status=(
                ModuleStatus.HEALTHY if self._initialized else ModuleStatus.UNHEALTHY
            ),
            latency_ms=0.0,
            last_event_time=None,
            details={"has_candidate": self._last_candidate is not None},
        )

    def propose(self, report: PerformanceReport, candidate_id: str) -> PolicyCandidate:
        """Create a candidate regime gate; production state is never modified.

        A regime with insufficient observations remains enabled at the base
        confidence. A well-sampled weak regime is tightened; a strong regime
        is allowed to use a lower threshold, but never below the safety floor.
        """
        regime_slices = [s for s in report.slices if s.key == "regime"]
        regimes: dict[str, RegimePolicy] = {}
        for item in regime_slices:
            if item.trades < self.min_trades:
                threshold = self.base_min_confidence
                enabled = True
            elif item.win_rate < 0.40 or item.average_r_multiple < -0.10:
                threshold = self.confidence_ceiling
                enabled = False
            elif item.win_rate >= 0.60 and item.average_r_multiple > 0.10:
                threshold = max(self.confidence_floor, self.base_min_confidence - 0.05)
                enabled = True
            else:
                threshold = self.base_min_confidence
                enabled = True
            regimes[item.value] = RegimePolicy(
                regime=item.value,
                enabled=enabled,
                min_confidence=round(threshold, 4),
                trades=item.trades,
                win_rate=round(item.win_rate, 4),
                average_r_multiple=round(item.average_r_multiple, 4),
            )

        candidate = PolicyCandidate(
            candidate_id=candidate_id,
            base_policy_version=self.base_policy_version,
            min_trades=self.min_trades,
            min_confidence_floor=self.confidence_floor,
            max_confidence_ceiling=self.confidence_ceiling,
            regimes=regimes,
        )
        self._last_candidate = candidate
        return candidate

    def last_candidate(self) -> PolicyCandidate | None:
        return self._last_candidate
