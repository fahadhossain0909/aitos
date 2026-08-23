"""BaseAgent — shared framework for every AITOS agent.

Concrete agents (MarketAgent, RiskAgent, PortfolioAgent, LearningAgent, ...)
subclass this and implement domain logic in ``handle_event`` / ``on_tick``.
The base class handles: lifecycle, event-bus wiring, short/long-term memory
access, weighted confidence scoring, and consensus participation — so
subclasses only write the decision logic that's actually specific to them.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from aitos.core.contracts import (AITOSModule, Event, EventResponse,
                                  HealthStatus, ModuleStatus)
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.eventbus.redis_bus import EventBus
from aitos.logging_setup import get_logger

logger = get_logger("aitos.agents")


class AgentMemory:
    """Minimal short/long-term memory store for an agent.

    This is an in-process placeholder. A production deployment would back
    short-term memory with Redis (hot state) and long-term memory with a
    vector store / ClickHouse (per the Infrastructure Stack), but the
    interface below is what the rest of AITOS depends on, so swapping the
    backing store later doesn't ripple through agent code.
    """

    def __init__(self, short_term_capacity: int = 500) -> None:
        self._short_term_capacity = short_term_capacity
        self._short_term: List[Dict[str, Any]] = []
        self._long_term: Dict[str, Any] = {}

    def remember_short_term(self, item: Dict[str, Any]) -> None:
        self._short_term.append(
            {**item, "_recorded_at": datetime.now(timezone.utc).isoformat()}
        )
        if len(self._short_term) > self._short_term_capacity:
            self._short_term.pop(0)

    def recent(self, n: int = 10) -> List[Dict[str, Any]]:
        return self._short_term[-n:]

    def remember_long_term(self, key: str, value: Any) -> None:
        self._long_term[key] = value

    def recall_long_term(self, key: str, default: Any = None) -> Any:
        return self._long_term.get(key, default)


class AgentDecision:
    """Standard shape for an agent's contribution to the Decision Fusion Engine."""

    __slots__ = (
        "agent_id",
        "confidence",
        "direction",
        "rationale",
        "evidence",
        "metadata",
    )

    def __init__(
        self,
        agent_id: str,
        confidence: float,
        direction: str,
        rationale: str,
        evidence: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be within [0.0, 1.0]")
        self.agent_id = agent_id
        self.confidence = confidence
        self.direction = direction  # "long" | "short" | "neutral"
        self.rationale = rationale
        self.evidence = evidence or []
        self.metadata = metadata or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "confidence": self.confidence,
            "direction": self.direction,
            "rationale": self.rationale,
            "evidence": self.evidence,
            "metadata": self.metadata,
        }


class BaseAgent(AITOSModule):
    """Base framework for all AI agents.

    - Unique agent identity (``module_id``)
    - Weighted confidence scoring (``consensus_weight``)
    - Memory access (short/long term) via ``self.memory``
    - Event subscription model via the injected ``EventBus``
    - Consensus participation via ``contribute_decision``
    """

    def __init__(
        self, agent_id: str, event_bus: EventBus, consensus_weight: float = 1.0
    ) -> None:
        self._agent_id = agent_id
        self._event_bus = event_bus
        self._consensus_weight = consensus_weight
        self._initialized = False
        self._last_event_time: Optional[str] = None
        self.memory = AgentMemory()

    # -- AITOSModule contract -------------------------------------------------

    @property
    def module_id(self) -> str:
        return self._agent_id

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def consensus_weight(self) -> float:
        return self._consensus_weight

    async def initialize(self, config: Dict[str, Any]) -> None:
        if self._initialized:
            return
        await self.on_initialize(config)
        self._initialized = True
        logger.info(
            "agent initialized", extra={"aitos_extra": {"agent_id": self.module_id}}
        )

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            module_id=self.module_id,
            status=(
                ModuleStatus.HEALTHY if self._initialized else ModuleStatus.UNHEALTHY
            ),
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={"consensus_weight": self._consensus_weight},
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        await self.on_shutdown(grace_period_seconds)
        logger.info(
            "agent shut down", extra={"aitos_extra": {"agent_id": self.module_id}}
        )

    async def emit_events(self) -> AsyncIterator[Event]:
        async for event in self.on_emit_events():
            self._last_event_time = datetime.now(timezone.utc).isoformat()
            yield event

    async def handle_event(self, event: Event) -> Optional[EventResponse]:
        if not self._initialized:
            raise ModuleNotInitializedError(f"Agent {self.module_id} not initialized")
        self._last_event_time = datetime.now(timezone.utc).isoformat()
        self.memory.remember_short_term(
            {"event_topic": event.topic, "event_id": event.event_id}
        )
        start = time.monotonic()
        try:
            return await self.on_handle_event(event)
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.debug(
                "event handled",
                extra={
                    "aitos_extra": {
                        "agent_id": self.module_id,
                        "topic": event.topic,
                        "elapsed_ms": elapsed_ms,
                    }
                },
            )

    # -- Consensus participation --------------------------------------------

    @abstractmethod
    async def contribute_decision(self, context: Dict[str, Any]) -> AgentDecision:
        """Produce this agent's weighted opinion for the Decision Fusion Engine."""

    # -- Hooks for subclasses (override what you need; defaults are no-ops) --

    async def on_initialize(self, config: Dict[str, Any]) -> None:
        return None

    async def on_shutdown(self, grace_period_seconds: float) -> None:
        return None

    async def on_emit_events(self) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover - makes this an async generator

    async def on_handle_event(self, event: Event) -> Optional[EventResponse]:
        return None
