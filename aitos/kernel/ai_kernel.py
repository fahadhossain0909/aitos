"""AI Kernel — central orchestrator of AITOS."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aitos.agents.base_agent import AgentDecision, BaseAgent
from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.core.exceptions import (
    AgentNotRegisteredError,
    DecisionFusionError,
    GovernanceViolationError,
    ModuleNotInitializedError,
)
from aitos.eventbus.redis_bus import EventBus
from aitos.intelligence.contextual_decision import ContextualDecisionEngine
from aitos.journal.policy_registry import PolicyRegistry
from aitos.kernel.decision_fusion import DEFAULT_EVIDENCE_WEIGHTS, DecisionFusionEngine
from aitos.logging_setup import get_logger

logger = get_logger("aitos.kernel")


@dataclass
class WorldState:
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    active_symbols: list[str] = field(default_factory=list)
    open_positions: dict[str, Any] = field(default_factory=dict)
    risk_score: float = 0.0
    regime: str = "unknown"
    registered_agents: list[str] = field(default_factory=list)


@dataclass
class DecisionContext:
    symbol: str
    context: dict[str, Any] = field(default_factory=dict)
    requested_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class FusedDecision:
    symbol: str
    direction: str
    confidence: float
    contributions: list[dict[str, Any]]
    conflicting_evidence: list[str]
    fused_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "confidence": self.confidence,
            "evidence_contributions": self.contributions,
            "conflicting_evidence": self.conflicting_evidence,
            "fused_at": self.fused_at,
        }


@dataclass
class Action:
    action_type: str
    payload: dict[str, Any]
    is_production: bool = True
    approved_by: str | None = None


@dataclass
class GovernanceResult:
    approved: bool
    reason: str
    requires_human_approval: bool


class AIKernel(AITOSModule):
    def __init__(
        self,
        event_bus: EventBus,
        require_human_approval_for_prod: bool = True,
        fusion_engine: DecisionFusionEngine | None = None,
        policy_registry: PolicyRegistry | None = None,
        contextual_engine: ContextualDecisionEngine | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._require_human_approval_for_prod = require_human_approval_for_prod
        self._fusion_engine = fusion_engine or DecisionFusionEngine()
        self._contextual_engine = contextual_engine or ContextualDecisionEngine()
        self._policy_registry = policy_registry or PolicyRegistry(
            os.getenv("AITOS_ACTIVE_POLICY_PATH", "runtime/active_policy.json"),
            DEFAULT_EVIDENCE_WEIGHTS,
        )
        self._initialized = False
        self._agents: dict[str, BaseAgent] = {}
        self._world_state = WorldState()
        self._last_event_time: str | None = None
        self._policy_version = "baseline"

    @property
    def module_id(self) -> str:
        return "ai-kernel"

    @property
    def version(self) -> str:
        return "1.5.0"

    @property
    def fusion_min_confidence(self) -> float:
        return self._fusion_engine.min_confidence

    @property
    def policy_version(self) -> str:
        return self._policy_version

    @property
    def fusion_weights(self) -> dict[str, float]:
        return self._fusion_engine.weights

    @property
    def contextual_engine(self) -> ContextualDecisionEngine:
        return self._contextual_engine

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        self._initialized = True
        self.reload_active_policy()
        logger.info(
            "AIKernel initialized",
            extra={"aitos_extra": {"policy_version": self._policy_version}},
        )

    def reload_active_policy(self) -> Any:
        """Load the already-governed active policy into the live fusion engine."""
        policy = self._policy_registry.active
        self._fusion_engine = DecisionFusionEngine(
            weights=policy.weights, min_confidence=policy.min_confidence
        )
        self._policy_version = policy.version
        return policy

    def apply_policy(self, policy: Any) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "AIKernel.initialize() must be called first"
            )
        self._fusion_engine = DecisionFusionEngine(
            weights=policy.weights, min_confidence=policy.min_confidence
        )
        self._policy_version = policy.version
        logger.info(
            "AIKernel policy activated",
            extra={
                "aitos_extra": {
                    "policy_version": policy.version,
                    "weights": policy.weights,
                    "min_confidence": policy.min_confidence,
                }
            },
        )

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            module_id=self.module_id,
            status=(
                ModuleStatus.HEALTHY if self._initialized else ModuleStatus.UNHEALTHY
            ),
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={
                "registered_agents": list(self._agents.keys()),
                "fusion_min_confidence": self.fusion_min_confidence,
                "policy_version": self._policy_version,
                "contextual_engine": True,
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        for agent in list(self._agents.values()):
            await agent.shutdown(grace_period_seconds)
        self._agents.clear()
        logger.info("AIKernel shut down")

    async def emit_events(self) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> EventResponse | None:
        self._last_event_time = datetime.now(timezone.utc).isoformat()
        self._update_world_state_from_event(event)
        for agent in self._agents.values():
            await agent.handle_event(event)
        return None

    async def register_agent(self, agent: BaseAgent) -> None:
        self._require_initialized()
        self._agents[agent.module_id] = agent
        self._world_state.registered_agents = list(self._agents.keys())

    async def deregister_agent(self, agent_id: str) -> None:
        self._require_initialized()
        if agent_id not in self._agents:
            raise AgentNotRegisteredError(f"Agent '{agent_id}' is not registered")
        del self._agents[agent_id]
        self._world_state.registered_agents = list(self._agents.keys())

    async def get_world_state(self) -> WorldState:
        self._require_initialized()
        return self._world_state

    def _contextual_evidence(
        self, context: DecisionContext
    ) -> tuple[dict[str, Any], list[str]]:
        payload = context.context
        direction = payload.get("direction")
        scores = payload.get("component_scores")
        if not isinstance(direction, str) or not isinstance(scores, dict):
            return {}, []
        try:
            advanced = payload.get("advanced_context")
            # Advanced context is accepted as an optional precomputed object.
            result = self._contextual_engine.build(
                direction=direction,
                component_scores=scores,
                component_availability=payload.get("component_availability"),
                context=payload,
                advanced=advanced if hasattr(advanced, "features") else None,
            )
            return {
                "source": "contextual_decision",
                "action": result.action,
                "confidence": result.confidence,
                "market_state": result.market_state,
                "scenarios": [x.to_dict() for x in result.scenarios],
                "evidence": [x.to_dict() for x in result.evidence],
                "target_zones": list(result.target_zones),
            }, list(result.contradictions)
        except Exception as exc:
            logger.warning(
                "contextual decision enrichment failed",
                extra={"aitos_extra": {"symbol": context.symbol, "error": str(exc)}},
            )
            return {}, []

    async def request_decision(self, context: DecisionContext) -> FusedDecision:
        self._require_initialized()
        evidence = self._fusion_engine.fuse_context(context.context)
        contextual, contextual_conflicts = self._contextual_evidence(context)
        if evidence is not None:
            contributions = [
                contribution.to_dict() for contribution in evidence.contributions
            ]
            if contextual:
                contributions.append(contextual)
            if evidence.missing_components:
                contributions.append(
                    {
                        "source": "missing_components",
                        "components": list(evidence.missing_components),
                    }
                )
            conflicts = list(contextual_conflicts)
            if evidence.direction == "neutral" and evidence.missing_components:
                conflicts.append(
                    f"insufficient evidence: {', '.join(evidence.missing_components)}"
                )
            if (
                contextual.get("action") == "no_trade"
                and evidence.direction != "neutral"
            ):
                conflicts.append("contextual layer recommends no_trade")
            return FusedDecision(
                symbol=context.symbol,
                direction=evidence.direction,
                confidence=evidence.confidence,
                contributions=contributions,
                conflicting_evidence=conflicts,
            )

        if not self._agents:
            raise DecisionFusionError(
                "No agents registered and no component evidence supplied"
            )
        decisions: list[AgentDecision] = []
        for agent in self._agents.values():
            try:
                decisions.append(
                    await agent.contribute_decision(
                        context.context | {"symbol": context.symbol}
                    )
                )
            except Exception as exc:
                logger.error(
                    "agent failed to contribute decision",
                    extra={
                        "aitos_extra": {"agent_id": agent.module_id, "error": str(exc)}
                    },
                )
        if not decisions:
            raise DecisionFusionError("All agents failed to contribute a decision")

        direction_scores: dict[str, float] = {"long": 0.0, "short": 0.0, "neutral": 0.0}
        total_weight = 0.0
        for decision in decisions:
            agent = self._agents[decision.agent_id]
            direction_scores[decision.direction] = (
                direction_scores.get(decision.direction, 0.0)
                + agent.consensus_weight * decision.confidence
            )
            total_weight += agent.consensus_weight
        fused_direction = max(direction_scores, key=direction_scores.get)
        fused_confidence = (
            direction_scores[fused_direction] / total_weight
            if total_weight > 0
            else 0.0
        )
        conflicting = [
            f"{decision.agent_id} voted {decision.direction} ({decision.confidence:.2f}): {decision.rationale}"
            for decision in decisions
            if decision.direction != fused_direction
        ]
        conflicting.extend(contextual_conflicts)
        if contextual.get("action") == "no_trade":
            conflicting.append("contextual layer recommends no_trade")
        return FusedDecision(
            symbol=context.symbol,
            direction=fused_direction,
            confidence=round(min(fused_confidence, 1.0), 4),
            contributions=[decision.to_dict() for decision in decisions]
            + ([contextual] if contextual else []),
            conflicting_evidence=conflicting,
        )

    async def enforce_governance(self, action: Action) -> GovernanceResult:
        self._require_initialized()
        if (
            action.is_production
            and self._require_human_approval_for_prod
            and not action.approved_by
        ):
            return GovernanceResult(
                False,
                "Production action requires explicit human approval (approved_by is empty).",
                True,
            )
        return GovernanceResult(True, "Approved.", False)

    async def require_approval_or_raise(self, action: Action) -> None:
        result = await self.enforce_governance(action)
        if not result.approved:
            raise GovernanceViolationError(result.reason)

    def _update_world_state_from_event(self, event: Event) -> None:
        self._world_state.updated_at = datetime.now(timezone.utc).isoformat()
        symbol = event.payload.get("symbol")
        if symbol and symbol not in self._world_state.active_symbols:
            self._world_state.active_symbols.append(symbol)
        if event.topic.startswith("risk.score") and isinstance(
            event.payload.get("score"), (int, float)
        ):
            self._world_state.risk_score = float(event.payload["score"])
        if event.topic.startswith("regime.") and isinstance(
            event.payload.get("regime"), str
        ):
            self._world_state.regime = event.payload["regime"]

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "AIKernel.initialize() must be called first"
            )
