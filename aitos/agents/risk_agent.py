"""RiskAgent — monitors risk scores, recommends position sizing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from aitos.agents.base_agent import AgentDecision, BaseAgent
from aitos.core.contracts import Event, EventResponse


class RiskAgent(BaseAgent):
    """Monitors risk engine events and recommends position sizing adjustments.

    Listens for ``risk.score_update`` and ``risk.emergency_stop`` events,
    tracks risk history in short-term memory, and contributes conservative
    sizing recommendations to the fusion engine.
    """

    # Risk action tiers
    TIER_NORMAL = "NORMAL"
    TIER_REDUCE = "REDUCE_SIZE"
    TIER_NO_NEW = "NO_NEW_ENTRIES"
    TIER_EMERGENCY = "EMERGENCY_STOP"

    TIER_THRESHOLDS = {
        TIER_NORMAL: 0.0,
        TIER_REDUCE: 70.0,
        TIER_NO_NEW: 85.0,
        TIER_EMERGENCY: 95.0,
    }

    def __init__(self, event_bus: Any, consensus_weight: float = 1.5) -> None:
        super().__init__(
            agent_id="risk-agent",
            event_bus=event_bus,
            consensus_weight=consensus_weight,
        )
        self._current_score: float = 0.0
        self._current_tier: str = self.TIER_NORMAL
        self._score_history: list[tuple[str, float]] = []  # (timestamp, score)

    async def on_initialize(self, config: dict[str, Any]) -> None:
        self.memory.remember_long_term(
            "risk_threshold_reduce", config.get("risk_threshold_reduce", 70.0)
        )
        self.memory.remember_long_term(
            "risk_threshold_no_new", config.get("risk_threshold_no_new", 85.0)
        )
        self.memory.remember_long_term(
            "risk_threshold_emergency", config.get("risk_threshold_emergency", 95.0)
        )
        self.memory.remember_long_term(
            "max_position_size", config.get("max_position_size", 0.1)
        )

    async def on_handle_event(self, event: Event) -> EventResponse | None:
        """Process risk-related events."""
        if event.topic.startswith("risk.score_update"):
            score = event.payload.get("score")
            if isinstance(score, (int, float)):
                self._current_score = float(score)
                self._score_history.append(
                    (event.payload.get("timestamp", ""), float(score))
                )
                if len(self._score_history) > 100:
                    self._score_history = self._score_history[-100:]
                self._current_tier = self._classify_tier(float(score))
                self.memory.remember_short_term(
                    {
                        "type": "risk_update",
                        "score": float(score),
                        "tier": self._current_tier,
                    }
                )
        elif event.topic == "risk.emergency_stop":
            self._current_tier = self.TIER_EMERGENCY
            self.memory.remember_short_term(
                {
                    "type": "emergency_stop",
                    "reason": event.payload.get("reason", "unknown"),
                }
            )
        return None

    async def on_tick(self) -> None:
        """Periodic risk re-evaluation."""

    async def on_emit_events(self) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover

    def _classify_tier(self, score: float) -> str:
        emergency = self.memory.recall_long_term("risk_threshold_emergency", 95.0)
        no_new = self.memory.recall_long_term("risk_threshold_no_new", 85.0)
        reduce = self.memory.recall_long_term("risk_threshold_reduce", 70.0)
        if score >= emergency:
            return self.TIER_EMERGENCY
        elif score >= no_new:
            return self.TIER_NO_NEW
        elif score >= reduce:
            return self.TIER_REDUCE
        return self.TIER_NORMAL

    def _compute_position_size(self, score: float) -> float:
        """Compute recommended position size as fraction of portfolio."""
        max_size = self.memory.recall_long_term("max_position_size", 0.1)
        tier = self._classify_tier(score)
        multipliers = {
            self.TIER_NORMAL: 1.0,
            self.TIER_REDUCE: 0.5,
            self.TIER_NO_NEW: 0.0,
            self.TIER_EMERGENCY: 0.0,
        }
        return max_size * multipliers.get(tier, 0.0)

    async def contribute_decision(self, context: dict[str, Any]) -> AgentDecision:
        """Produce a risk-aware decision for the fusion engine."""
        tier = self._current_tier
        score = self._current_score
        position_size = self._compute_position_size(score)

        if tier == self.TIER_EMERGENCY:
            direction = "neutral"
            confidence = 1.0
            rationale = (
                f"EMERGENCY: risk score {score:.1f} >= threshold. No new entries."
            )
        elif tier == self.TIER_NO_NEW:
            direction = "neutral"
            confidence = 0.9
            rationale = f"Risk score {score:.1f} in NO_NEW_ENTRIES zone. Hold only."
        elif tier == self.TIER_REDUCE:
            direction = "neutral"
            confidence = 0.7
            rationale = f"Risk score {score:.1f} elevated. Reduce position size to {position_size:.1%}."
        else:
            direction = context.get("direction", "neutral")
            confidence = 0.8
            rationale = (
                f"Risk normal ({score:.1f}). Max position size {position_size:.1%}."
            )

        evidence = [
            f"current_score={score:.1f}",
            f"tier={tier}",
            f"recommended_size={position_size:.1%}",
        ]
        if self._score_history:
            recent_scores = [s for _, s in self._score_history[-10:]]
            evidence.append(f"recent_avg={sum(recent_scores) / len(recent_scores):.1f}")

        return AgentDecision(
            agent_id=self.module_id,
            confidence=confidence,
            direction=direction,
            rationale=rationale,
            evidence=evidence,
            metadata={
                "risk_score": score,
                "tier": tier,
                "recommended_position_size": position_size,
            },
        )
