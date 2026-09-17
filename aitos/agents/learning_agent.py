"""LearningAgent — analyzes past trade outcomes, adjusts strategy parameters."""

from __future__ import annotations

import statistics
from collections.abc import AsyncIterator
from typing import Any

from aitos.agents.base_agent import AgentDecision, BaseAgent
from aitos.core.contracts import Event, EventResponse


class LearningAgent(BaseAgent):
    """Analyzes past trade outcomes and adjusts strategy parameters.

    Listens for ``trade.closed`` and ``learning.experience`` events,
    tracks outcome history in short-term memory, and contributes
    meta-learning decisions (e.g., adjust thresholds, disable components)
    to the fusion engine.
    """

    def __init__(self, event_bus: Any, consensus_weight: float = 0.8) -> None:
        super().__init__(
            agent_id="learning-agent",
            event_bus=event_bus,
            consensus_weight=consensus_weight,
        )
        self._outcomes: list[dict[str, Any]] = []  # past trade outcomes
        self._strategy_adjustments: dict[str, Any] = {}
        self._component_performance: dict[str, list[float]] = {}
        self._regime_performance: dict[str, list[float]] = {}

    async def on_initialize(self, config: dict[str, Any]) -> None:
        self.memory.remember_long_term("min_samples", config.get("min_samples", 10))
        self.memory.remember_long_term(
            "adjustment_rate", config.get("adjustment_rate", 0.1)
        )
        self.memory.remember_long_term(
            "lookback_trades", config.get("lookback_trades", 50)
        )

    async def on_handle_event(self, event: Event) -> EventResponse | None:
        """Process learning-related events."""
        if event.topic.startswith("trade.closed"):
            outcome = {
                "symbol": event.payload.get("symbol"),
                "side": event.payload.get("side"),
                "pnl": event.payload.get("realized_pnl", 0.0),
                "duration_minutes": event.payload.get("duration_minutes", 0),
                "entry_price": event.payload.get("entry_price", 0.0),
                "exit_price": event.payload.get("exit_price", 0.0),
                "timestamp": event.payload.get("timestamp", ""),
                "strategy": event.payload.get("strategy", "unknown"),
                "regime": event.payload.get("regime", "unknown"),
            }
            self._outcomes.append(outcome)
            lookback = self.memory.recall_long_term("lookback_trades", 50)
            if len(self._outcomes) > lookback:
                self._outcomes = self._outcomes[-lookback:]

            # Track per-regime performance
            regime = outcome.get("regime", "unknown")
            pnl = outcome.get("pnl", 0.0)
            if isinstance(pnl, (int, float)):
                if regime not in self._regime_performance:
                    self._regime_performance[regime] = []
                self._regime_performance[regime].append(float(pnl))

            self.memory.remember_short_term(
                {
                    "type": "trade_outcome",
                    "pnl": pnl,
                    "regime": regime,
                }
            )
        elif event.topic.startswith("learning.experience"):
            component = event.payload.get("component")
            score = event.payload.get("score")
            if component and isinstance(score, (int, float)):
                if component not in self._component_performance:
                    self._component_performance[component] = []
                self._component_performance[component].append(float(score))
                if len(self._component_performance[component]) > 100:
                    self._component_performance[component] = (
                        self._component_performance[component][-100:]
                    )
                self.memory.remember_short_term(
                    {
                        "type": "learning_experience",
                        "component": component,
                        "score": float(score),
                    }
                )
        return None

    async def on_tick(self) -> None:
        """Periodic analysis of outcomes to update strategy adjustments."""
        if len(self._outcomes) >= self.memory.recall_long_term("min_samples", 10):
            self._update_strategy_adjustments()

    async def on_emit_events(self) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover

    def _update_strategy_adjustments(self) -> None:
        """Analyze outcomes and update strategy parameters."""
        if not self._outcomes:
            return
        pnls = [
            o.get("pnl", 0.0)
            for o in self._outcomes
            if isinstance(o.get("pnl"), (int, float))
        ]
        if not pnls:
            return
        avg_pnl = statistics.mean(pnls)
        if len(pnls) >= 2:
            pnl_volatility = statistics.stdev(pnls)
        else:
            pnl_volatility = 0.0
        win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
        adjustment_rate = self.memory.recall_long_term("adjustment_rate", 0.1)

        self._strategy_adjustments = {
            "suggested_confidence_modifier": round(min(win_rate, 1.0), 4),
            "suggested_size_modifier": round(
                max(
                    0.5,
                    1.0 - adjustment_rate * (pnl_volatility / (abs(avg_pnl) + 0.001)),
                ),
                4,
            ),
            "avg_pnl": round(avg_pnl, 4),
            "pnl_volatility": round(pnl_volatility, 4),
            "win_rate": round(win_rate, 4),
        }

    def _identify_best_regime(self) -> tuple[str, float]:
        """Identify the most profitable regime."""
        if not self._regime_performance:
            return ("unknown", 0.0)
        best_regime = "unknown"
        best_avg = float("-inf")
        for regime, pnls in self._regime_performance.items():
            if pnls:
                avg = statistics.mean(pnls)
                if avg > best_avg:
                    best_avg = avg
                    best_regime = regime
        return (best_regime, round(best_avg, 4))

    def _identify_worst_component(self) -> tuple[str, float]:
        """Identify the worst-performing component."""
        if not self._component_performance:
            return ("none", 0.0)
        worst = "none"
        worst_avg = float("inf")
        for comp, scores in self._component_performance.items():
            if len(scores) >= 3:
                avg = statistics.mean(scores)
                if avg < worst_avg:
                    worst_avg = avg
                    worst = comp
        return (worst, round(worst_avg, 4))

    async def contribute_decision(self, context: dict[str, Any]) -> AgentDecision:
        """Produce a meta-learning decision for the fusion engine."""
        if len(self._outcomes) < self.memory.recall_long_term("min_samples", 10):
            return AgentDecision(
                agent_id=self.module_id,
                confidence=0.3,
                direction="neutral",
                rationale=f"Insufficient data: {len(self._outcomes)} outcomes < min_samples={self.memory.recall_long_term('min_samples', 10)}.",
                evidence=[f"outcomes_collected={len(self._outcomes)}"],
            )

        best_regime, best_regime_pnl = self._identify_best_regime()
        worst_component, worst_component_score = self._identify_worst_component()
        pnls = [
            o.get("pnl", 0.0)
            for o in self._outcomes
            if isinstance(o.get("pnl"), (int, float))
        ]
        win_rate = sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0
        confidence = min(
            len(self._outcomes) / (self.memory.recall_long_term("min_samples", 10) * 3),
            1.0,
        )

        if worst_component != "none" and worst_component_score < 3.0:
            direction = "neutral"
            rationale = f"Disable weak component '{worst_component}' (avg_score={worst_component_score:.2f}). Best regime: {best_regime}."
        elif win_rate < 0.4:
            direction = "neutral"
            rationale = (
                f"Win rate {win_rate:.1%} below threshold. Recommend reducing exposure."
            )
        else:
            direction = "long" if best_regime_pnl > 0 else "neutral"
            rationale = f"Learning: win_rate={win_rate:.1%}, best_regime={best_regime}, avg_pnl={statistics.mean(pnls):.2f}."

        evidence = [
            f"total_outcomes={len(self._outcomes)}",
            f"win_rate={win_rate:.1%}",
            f"best_regime={best_regime}",
            f"worst_component={worst_component}",
        ]
        if self._strategy_adjustments:
            evidence.append(
                f"size_modifier={self._strategy_adjustments.get('suggested_size_modifier', 1.0)}"
            )

        return AgentDecision(
            agent_id=self.module_id,
            confidence=round(confidence, 4),
            direction=direction,
            rationale=rationale,
            evidence=evidence,
            metadata={
                "win_rate": win_rate,
                "best_regime": best_regime,
                "best_regime_pnl": best_regime_pnl,
                "worst_component": worst_component,
                "worst_component_score": worst_component_score,
                "strategy_adjustments": self._strategy_adjustments,
            },
        )
