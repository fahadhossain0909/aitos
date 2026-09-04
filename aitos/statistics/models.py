"""Public, serialisable contracts for AITOS predictive statistics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


@dataclass(frozen=True)
class BayesianEvidence:
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

    def normalised(self) -> RegimeProbability:
        values = tuple(
            _clip(v)
            for v in (
                self.trend_up,
                self.trend_down,
                self.range,
                self.high_volatility,
                self.low_volatility,
            )
        )
        total = sum(values)
        if total <= 0:
            return RegimeProbability(range=1.0)
        return RegimeProbability(*(v / total for v in values))


@dataclass(frozen=True)
class EVTTail:
    """Serializable Peaks-Over-Threshold extreme-tail estimate.

    ``tail_probability`` is the empirical probability of exceeding the fitted
    threshold. ``expected_shortfall`` is the estimated mean loss conditional on
    a threshold exceedance. Keeping the full estimator state here makes EVT
    output stable for stack consumers and replay/serialization paths.
    """

    threshold: float
    exceedances: int
    exceedance_rate: float
    shape: float
    scale: float
    tail_probability: float
    expected_shortfall: float

    def __post_init__(self) -> None:
        if self.exceedances < 0:
            raise ValueError("exceedances must be non-negative")
        for name in (
            "threshold",
            "exceedance_rate",
            "shape",
            "scale",
            "tail_probability",
            "expected_shortfall",
        ):
            value = float(getattr(self, name))
            if not __import__("math").isfinite(value):
                raise ValueError(f"{name} must be finite")
        if not 0.0 <= self.exceedance_rate <= 1.0:
            raise ValueError("exceedance_rate must be in [0, 1]")
        if not 0.0 <= self.tail_probability <= 1.0:
            raise ValueError("tail_probability must be in [0, 1]")
        if self.threshold < 0 or self.scale < 0 or self.expected_shortfall < 0:
            raise ValueError("EVT loss magnitudes must be non-negative")


@dataclass(frozen=True)
class AStatObservation:
    symbol: str
    features: dict[str, Any] = field(default_factory=dict)
    prior_up: float = 0.5
    prior_down: float = 0.5
    flat_threshold: float = 0.001
    horizon: str = "1h"
    sample_size: int = 0


@dataclass(frozen=True)
class StrategyStatContext:
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
        upside = self.direction.up
        edge = max(0.0, self.expected_value)
        penalty = 0.5 * self.downside_probability + 0.5 * self.tail_loss_probability
        return _clip(
            (0.55 * upside + 0.45 * min(1.0, edge * 10.0)) * self.probability_confidence
            - penalty
        )


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
    advanced: Any | None = None

    def for_strategy(self, strategy_id: str) -> StrategyStatContext:
        return StrategyStatContext(
            strategy_id,
            self.direction,
            self.regime,
            self.expected_return,
            self.expected_volatility,
            self.downside_probability,
            self.tail_loss_probability,
            self.expected_value,
            self.probability_confidence,
            self.calibration_quality,
            self.sample_size,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
