"""Public, serialisable contracts for AITOS predictive statistics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


@dataclass(frozen=True)
class BayesianEvidence:
    """Likelihood ratio supplied by an independent market observation."""

    name: str
    likelihood_ratio: float

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("evidence name must not be empty")
        if self.likelihood_ratio <= 0:
            raise ValueError("likelihood_ratio must be positive")


@dataclass(frozen=True)
class DirectionProbability:
    up: float
    down: float
    flat: float

    def __post_init__(self) -> None:
        values = tuple(_clip(v) for v in (self.up, self.down, self.flat))
        total = sum(values)
        if total <= 0:
            raise ValueError("direction probabilities must have positive mass")
        object.__setattr__(self, "up", values[0] / total)
        object.__setattr__(self, "down", values[1] / total)
        object.__setattr__(self, "flat", values[2] / total)


@dataclass(frozen=True)
class RegimeProbability:
    trend_up: float = 0.0
    trend_down: float = 0.0
    range: float = 0.0
    high_volatility: float = 0.0
    low_volatility: float = 0.0

    def normalised(self) -> "RegimeProbability":
        values = tuple(_clip(v) for v in (
            self.trend_up,
            self.trend_down,
            self.range,
            self.high_volatility,
            self.low_volatility,
        ))
        total = sum(values)
        if total <= 0:
            return RegimeProbability(range=1.0)
        return RegimeProbability(*(v / total for v in values))


@dataclass(frozen=True)
class AStatObservation:
    """Market-neutral statistical inputs; values are intentionally generic."""

    symbol: str
    features: dict[str, float] = field(default_factory=dict)
    prior_up: float = 0.5
    prior_down: float = 0.5
    flat_threshold: float = 0.001
    horizon: str = "1h"
    sample_size: int = 0


@dataclass(frozen=True)
class StrategyStatContext:
    """Stable boundary consumed by any AITOS strategy family."""

    strategy_id: str
    direction: DirectionProbability
    regime: RegimeProbability
    expected_return: float
    expected_volatility: float
    downside_probability: float
    tail_loss_probability: float
    expected_value: float
    probability_confidence: float
    calibration_quality: float
    sample_size: int

    @property
    def suitability(self) -> float:
        """Risk-adjusted statistical attractiveness in [0, 1]."""
        upside = self.direction.up
        edge = max(0.0, self.expected_value)
        penalty = 0.5 * self.downside_probability + 0.5 * self.tail_loss_probability
        return _clip((0.55 * upside + 0.45 * min(1.0, edge * 10.0)) * self.probability_confidence - penalty)


@dataclass(frozen=True)
class AStatResult:
    symbol: str
    horizon: str
    direction: DirectionProbability
    regime: RegimeProbability
    expected_return: float
    expected_volatility: float
    downside_probability: float
    tail_loss_probability: float
    expected_value: float
    probability_confidence: float
    calibration_quality: float
    sample_size: int
    evidence: tuple[str, ...] = ()

    def for_strategy(self, strategy_id: str) -> StrategyStatContext:
        return StrategyStatContext(
            strategy_id=strategy_id,
            direction=self.direction,
            regime=self.regime,
            expected_return=self.expected_return,
            expected_volatility=self.expected_volatility,
            downside_probability=self.downside_probability,
            tail_loss_probability=self.tail_loss_probability,
            expected_value=self.expected_value,
            probability_confidence=self.probability_confidence,
            calibration_quality=self.calibration_quality,
            sample_size=self.sample_size,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
