"""Trade Journey — stateful in-trade management intelligence.

The journey layer sits between entry and final exit.  It deliberately does not
replace the existing Exit Intelligence Engine (EIE): it adds temporal state,
trade health, expected-vs-actual path adherence, and an explicit management
action so a position can be held, protected, reduced, hedged, or exited for a
reason that is independent of a static TP/SL.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from math import isfinite


class TradeJourneyState(str, Enum):
    PROVING = "PROVING"
    CONFIRMED = "CONFIRMED"
    EXTENDING = "EXTENDING"
    PROTECTING = "PROTECTING"
    DECAYING = "DECAYING"
    UNCERTAIN = "UNCERTAIN"
    EXITING = "EXITING"


class TradeJourneyAction(str, Enum):
    HOLD = "HOLD"
    PROTECT = "PROTECT"
    TRAIL = "TRAIL"
    REDUCE = "REDUCE"
    HEDGE = "HEDGE"
    EXIT = "EXIT"


@dataclass(frozen=True)
class TradeJourneySnapshot:
    state: TradeJourneyState
    action: TradeJourneyAction
    health_score: float
    thesis_health_score: float
    path_adherence: float
    momentum_score: float
    liquidity_score: float
    structure_score: float
    time_efficiency: float
    unrealized_r: float
    age_seconds: float
    expected_progress: float
    actual_progress: float
    uncertainty: float
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "action": self.action.value,
            "health_score": self.health_score,
            "thesis_health_score": self.thesis_health_score,
            "path_adherence": self.path_adherence,
            "momentum_score": self.momentum_score,
            "liquidity_score": self.liquidity_score,
            "structure_score": self.structure_score,
            "time_efficiency": self.time_efficiency,
            "unrealized_r": self.unrealized_r,
            "age_seconds": self.age_seconds,
            "expected_progress": self.expected_progress,
            "actual_progress": self.actual_progress,
            "uncertainty": self.uncertainty,
            "reasons": list(self.reasons),
        }


class TradeJourneyEngine:
    """Evaluate the current *journey* of an already-open trade.

    Scores are bounded to [0, 1].  This is intentionally a deterministic
    management layer; statistical calibration can later replace the heuristic
    thresholds without changing the PositionManager contract.
    """

    def __init__(
        self,
        *,
        proving_max_r: float = 0.50,
        healthy_threshold: float = 0.70,
        uncertain_threshold: float = 0.48,
        decay_threshold: float = 0.34,
        stale_after_seconds: float = 900.0,
        reduce_health_threshold: float = 0.42,
        max_reduce_fraction: float = 0.50,
    ) -> None:
        self.proving_max_r = max(0.0, proving_max_r)
        self.healthy_threshold = min(1.0, max(0.0, healthy_threshold))
        self.uncertain_threshold = min(
            self.healthy_threshold, max(0.0, uncertain_threshold)
        )
        self.decay_threshold = min(self.uncertain_threshold, max(0.0, decay_threshold))
        self.stale_after_seconds = max(1.0, stale_after_seconds)
        self.reduce_health_threshold = min(1.0, max(0.0, reduce_health_threshold))
        self.max_reduce_fraction = min(1.0, max(0.0, max_reduce_fraction))

    @staticmethod
    def _bounded(value: float | None, default: float = 0.5) -> float:
        if value is None or not isfinite(value):
            return default
        return min(1.0, max(0.0, value))

    @staticmethod
    def _path_adherence(
        *,
        side: str,
        current_price: float,
        entry_price: float,
        expected_path_prices: Sequence[float],
    ) -> tuple[float, float, float]:
        if not expected_path_prices or current_price <= 0 or entry_price <= 0:
            return 0.5, 0.0, 0.0
        direction = 1.0 if side == "LONG" else -1.0
        actual = max(0.0, (current_price - entry_price) * direction)
        distances = [
            max(0.0, (p - entry_price) * direction) for p in expected_path_prices
        ]
        distances = [d for d in distances if d > 0]
        if not distances:
            return 0.5, 0.0, 0.0
        target = max(distances)
        progress = min(1.0, actual / target) if target > 0 else 0.0
        # Reward progress through the planned path; penalise meaningful adverse
        # movement without requiring the price to have reached the destination.
        adverse = max(0.0, -(current_price - entry_price) * direction)
        adverse_ratio = min(1.0, adverse / max(target, 1e-12))
        adherence = max(0.0, min(1.0, 0.35 + 0.65 * progress - 0.65 * adverse_ratio))
        return adherence, progress, target

    def evaluate(
        self,
        *,
        side: str,
        entry_price: float,
        current_price: float,
        unrealized_r: float,
        age_seconds: float,
        thesis_health: float,
        momentum: float | None,
        liquidity: float | None,
        structure: float | None,
        expected_path_prices: Sequence[float] = (),
        expected_progress: float | None = None,
    ) -> TradeJourneySnapshot:
        thesis = self._bounded(thesis_health)
        momentum_score = self._bounded(momentum)
        liquidity_score = self._bounded(liquidity)
        structure_score = self._bounded(structure)
        adherence, actual_progress, _ = self._path_adherence(
            side=side,
            current_price=current_price,
            entry_price=entry_price,
            expected_path_prices=expected_path_prices,
        )
        if expected_progress is not None and isfinite(expected_progress):
            expected_progress = min(1.0, max(0.0, expected_progress))
        else:
            # A modest time-based expectation prevents a trade from being
            # considered healthy forever simply because its thesis is intact.
            expected_progress = min(
                1.0, max(0.0, age_seconds / self.stale_after_seconds)
            )

        time_efficiency = 1.0
        if expected_progress > 0:
            time_efficiency = min(1.0, actual_progress / expected_progress)
        if age_seconds > self.stale_after_seconds and actual_progress < 0.10:
            time_efficiency *= 0.35

        # Thesis/path dominate; microstructure confirms rather than overrides.
        health = (
            0.30 * thesis
            + 0.20 * adherence
            + 0.15 * momentum_score
            + 0.15 * liquidity_score
            + 0.10 * structure_score
            + 0.10 * time_efficiency
        )
        health = min(1.0, max(0.0, health))
        uncertainty = 1.0 - abs(health - 0.5) * 2.0
        reasons: list[str] = []
        if thesis < 0.35:
            reasons.append("thesis_weak")
        if adherence < 0.40:
            reasons.append("path_deviation")
        if momentum_score < 0.35:
            reasons.append("momentum_decay")
        if liquidity_score < 0.35:
            reasons.append("liquidity_deterioration")
        if time_efficiency < 0.35:
            reasons.append("time_inefficient")
        if age_seconds > self.stale_after_seconds and actual_progress < 0.10:
            reasons.append("stale_trade")

        if thesis < 0.25 or structure_score < 0.20:
            state = TradeJourneyState.EXITING
            action = TradeJourneyAction.EXIT
            reasons.append("thesis_or_structure_failure")
        elif health < self.decay_threshold:
            state = TradeJourneyState.DECAYING
            action = TradeJourneyAction.REDUCE
        elif health < self.uncertain_threshold:
            state = TradeJourneyState.UNCERTAIN
            action = TradeJourneyAction.HEDGE
        elif unrealized_r <= self.proving_max_r:
            state = TradeJourneyState.PROVING
            action = TradeJourneyAction.HOLD
        elif health >= self.healthy_threshold and actual_progress >= 0.50:
            state = TradeJourneyState.EXTENDING
            action = TradeJourneyAction.TRAIL
        elif health >= self.healthy_threshold:
            state = TradeJourneyState.CONFIRMED
            action = TradeJourneyAction.PROTECT
        else:
            state = TradeJourneyState.PROTECTING
            action = TradeJourneyAction.PROTECT

        if action == TradeJourneyAction.REDUCE and self.max_reduce_fraction <= 0:
            action = TradeJourneyAction.PROTECT

        return TradeJourneySnapshot(
            state=state,
            action=action,
            health_score=health * 100.0,
            thesis_health_score=thesis * 100.0,
            path_adherence=adherence * 100.0,
            momentum_score=momentum_score * 100.0,
            liquidity_score=liquidity_score * 100.0,
            structure_score=structure_score * 100.0,
            time_efficiency=time_efficiency * 100.0,
            unrealized_r=unrealized_r,
            age_seconds=max(0.0, age_seconds),
            expected_progress=expected_progress,
            actual_progress=actual_progress,
            uncertainty=uncertainty * 100.0,
            reasons=tuple(dict.fromkeys(reasons)),
        )
