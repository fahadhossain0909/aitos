"""Closed-loop capital feedback without weakening hard protection gates.

P2 deliberately stays model-agnostic. It records realized trade outcomes and
produces bounded diagnostics that can later be consumed by statistical,
Deep Learning, or RL models. It never changes a hard capital-protection rule
or authorizes a trade by itself.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from statistics import mean


@dataclass(frozen=True)
class CapitalFeedbackConfig:
    """Bounds for the online outcome-feedback window."""

    window_size: int = 500
    min_samples: int = 50
    probability_bins: int = 10


@dataclass(frozen=True)
class CapitalOutcome:
    """One closed trade outcome plus the decision-time evidence."""

    symbol: str
    realized_return_pct: float
    predicted_loss_probability: float
    predicted_net_edge_pct: float
    realized_cost_pct: float = 0.0
    regime: str = ""
    model_id: str = ""
    timestamp: str = ""

    @property
    def realized_loss(self) -> bool:
        return self.realized_return_pct < 0.0


@dataclass(frozen=True)
class CapitalFeedbackSnapshot:
    """Stable diagnostics exposed to telemetry and future learning systems."""

    sample_count: int
    win_rate: float
    mean_return_pct: float
    mean_edge_pct: float
    mean_cost_pct: float
    realized_loss_rate: float
    brier_score: float
    by_regime: dict[str, int]
    by_model: dict[str, int]


class CapitalFeedback:
    """Bounded online feedback ledger for post-trade learning.

    Hard protection remains authoritative. Feedback is observational and can
    only improve estimates/models outside the execution authorization path.
    """

    def __init__(self, config: CapitalFeedbackConfig | None = None) -> None:
        self.config = config or CapitalFeedbackConfig()
        if self.config.window_size < 1:
            raise ValueError("window_size must be positive")
        if self.config.min_samples < 1:
            raise ValueError("min_samples must be positive")
        if self.config.probability_bins < 2:
            raise ValueError("probability_bins must be at least 2")
        self._outcomes: deque[CapitalOutcome] = deque(maxlen=self.config.window_size)

    @staticmethod
    def _probability(value: float) -> float:
        if not isfinite(value):
            raise ValueError("predicted_loss_probability must be finite")
        return max(0.0, min(1.0, value))

    @staticmethod
    def _finite(value: float, name: str) -> float:
        if not isfinite(value):
            raise ValueError(f"{name} must be finite")
        return value

    def record(self, outcome: CapitalOutcome) -> None:
        """Record a realized outcome; malformed observations are rejected."""
        if not outcome.symbol:
            raise ValueError("symbol must be non-empty")
        self._finite(outcome.realized_return_pct, "realized_return_pct")
        self._finite(outcome.predicted_net_edge_pct, "predicted_net_edge_pct")
        self._finite(outcome.realized_cost_pct, "realized_cost_pct")
        self._probability(outcome.predicted_loss_probability)
        self._outcomes.append(outcome)

    def extend(self, outcomes: Iterable[CapitalOutcome]) -> None:
        for outcome in outcomes:
            self.record(outcome)

    @property
    def sample_count(self) -> int:
        return len(self._outcomes)

    def ready(self) -> bool:
        """Whether enough observations exist for learning/calibration use."""
        return self.sample_count >= self.config.min_samples

    def probability_calibration(self) -> dict[int, tuple[int, int, float]]:
        """Return empirical loss rates by probability bin.

        The result is intentionally diagnostic. Consumers must still respect
        the hard gates in ``CapitalGateway`` and ``CapitalProtection``.
        """
        bins: dict[int, list[int]] = {}
        for outcome in self._outcomes:
            probability = self._probability(outcome.predicted_loss_probability)
            index = min(
                self.config.probability_bins - 1,
                int(probability * self.config.probability_bins),
            )
            bucket = bins.setdefault(index, [0, 0])
            bucket[0] += 1
            bucket[1] += int(outcome.realized_loss)
        return {
            index: (count, losses, losses / count)
            for index, (count, losses) in bins.items()
            if count
        }

    def snapshot(self) -> CapitalFeedbackSnapshot:
        if not self._outcomes:
            return CapitalFeedbackSnapshot(0, 0.0, 0.0, 0.0, 0.0, 0.0, {}, {})

        outcomes = tuple(self._outcomes)
        by_regime: dict[str, int] = {}
        by_model: dict[str, int] = {}
        brier_total = 0.0
        for outcome in outcomes:
            by_regime[outcome.regime] = by_regime.get(outcome.regime, 0) + 1
            by_model[outcome.model_id] = by_model.get(outcome.model_id, 0) + 1
            target = 1.0 if outcome.realized_loss else 0.0
            probability = self._probability(outcome.predicted_loss_probability)
            brier_total += (probability - target) ** 2

        return CapitalFeedbackSnapshot(
            sample_count=len(outcomes),
            win_rate=sum(not item.realized_loss for item in outcomes) / len(outcomes),
            mean_return_pct=mean(item.realized_return_pct for item in outcomes),
            mean_edge_pct=mean(item.predicted_net_edge_pct for item in outcomes),
            mean_cost_pct=mean(item.realized_cost_pct for item in outcomes),
            realized_loss_rate=sum(item.realized_loss for item in outcomes)
            / len(outcomes),
            brier_score=brier_total / len(outcomes),
            by_regime=by_regime,
            by_model=by_model,
        )

    def calibration_ready(self, minimum: int | None = None) -> bool:
        required = self.config.min_samples if minimum is None else minimum
        return self.sample_count >= required

    @staticmethod
    def now_iso() -> str:
        """UTC timestamp helper for outcome sinks."""
        return datetime.now(timezone.utc).isoformat()
