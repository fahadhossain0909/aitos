"""Strategy-family views over the shared A-Stat result.

The router does not place orders. It ranks the statistical suitability of
strategy families so the existing strategy/risk pipeline can make the final
execution decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .models import AStatResult


@dataclass(frozen=True)
class StrategyScore:
    strategy_id: str
    score: float
    rationale: tuple[str, ...]


class StatisticalStrategyRouter:
    """Convert one market probability state into strategy-specific scores."""

    def rank(self, result: AStatResult) -> tuple[StrategyScore, ...]:
        direction = result.direction
        regime = result.regime
        confidence = result.probability_confidence

        directional = (
            0.45 * max(direction.up, direction.down)
            + 0.25 * max(regime.trend_up, regime.trend_down)
            + 0.20 * max(0.0, min(1.0, result.expected_value * 10.0))
            + 0.10 * confidence
        )
        hedge = (
            0.45 * result.downside_probability
            + 0.25 * result.tail_loss_probability
            + 0.15 * regime.high_volatility
            + 0.15 * confidence
        )
        options = (
            0.40 * result.expected_volatility / max(result.expected_volatility, 0.01)
            + 0.30 * abs(result.expected_return) / max(result.expected_volatility, 0.01)
            + 0.20 * confidence
            + 0.10 * (1.0 - result.direction.flat)
        )

        scores = (
            StrategyScore("directional", min(1.0, directional), self._directional_rationale(result)),
            StrategyScore("hedging", min(1.0, hedge), self._hedge_rationale(result)),
            StrategyScore("options", min(1.0, options), self._options_rationale(result)),
        )
        return tuple(sorted(scores, key=lambda item: item.score, reverse=True))

    @staticmethod
    def _directional_rationale(result: AStatResult) -> tuple[str, ...]:
        side = "up" if result.direction.up >= result.direction.down else "down"
        return (
            f"direction_{side}={max(result.direction.up, result.direction.down):.3f}",
            f"expected_value={result.expected_value:.6f}",
            f"confidence={result.probability_confidence:.3f}",
        )

    @staticmethod
    def _hedge_rationale(result: AStatResult) -> tuple[str, ...]:
        return (
            f"downside={result.downside_probability:.3f}",
            f"tail={result.tail_loss_probability:.3f}",
            f"high_vol_regime={result.regime.high_volatility:.3f}",
        )

    @staticmethod
    def _options_rationale(result: AStatResult) -> tuple[str, ...]:
        return (
            f"volatility={result.expected_volatility:.6f}",
            f"expected_return={result.expected_return:.6f}",
            f"flat_probability={result.direction.flat:.3f}",
        )


def strategy_contexts(result: AStatResult, strategy_ids: Mapping[str, str] | None = None) -> dict[str, object]:
    """Expose the same statistical state to named strategy implementations."""
    ids = strategy_ids or {"directional": "directional", "hedging": "hedging", "options": "options"}
    return {family: result.for_strategy(strategy_id) for family, strategy_id in ids.items()}
