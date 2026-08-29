"""AttentionFeedbackLoop — trains ``AttentionExplainer`` online from real
closed trades. Same Event-Bus-subscription pattern as
``MLExplainerFeedbackLoop``/``RLFeedbackLoop``/``JournalSystem`` — no
manual training step, no direct coupling to the Trade Lifecycle.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.logging_setup import get_logger
from aitos.xai.attention_explainer import AttentionExplainer

logger = get_logger("aitos.xai.attention_feedback")


class AttentionFeedbackLoop(AITOSModule):
    def __init__(self, event_bus: EventBus, explainer: AttentionExplainer) -> None:
        self._event_bus = event_bus
        self._explainer = explainer
        self._initialized = False
        self._subscriptions: list[Subscription] = []
        self._updates_applied = 0
        self._last_event_time: str | None = None

    @property
    def module_id(self) -> str:
        return "attention-feedback-loop"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        self._subscriptions.append(
            await self._event_bus.subscribe(
                "trade.position_closed",
                self._on_position_closed,
                group="attention-feedback",
            )
        )
        self._initialized = True
        logger.info("AttentionFeedbackLoop initialized")

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            module_id=self.module_id,
            status=(
                ModuleStatus.HEALTHY if self._initialized else ModuleStatus.UNHEALTHY
            ),
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={
                "updates_applied": self._updates_applied,
                "explainer_ready": self._explainer.is_ready,
                "samples_seen": self._explainer.n_samples_seen,
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        for sub in self._subscriptions:
            sub.cancel()
        self._subscriptions.clear()
        logger.info("AttentionFeedbackLoop shut down")

    async def emit_events(self) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    @property
    def updates_applied(self) -> int:
        return self._updates_applied

    async def _on_position_closed(self, event: Event) -> EventResponse | None:
        trade_dict = event.payload
        pnl = trade_dict.get("pnl")
        agent_consensus = trade_dict.get("agent_consensus")
        if pnl is None or not agent_consensus:
            return None

        self._explainer.partial_fit(agent_consensus, won=pnl > 0)
        self._updates_applied += 1
        self._last_event_time = event.created_at
        return None

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "AttentionFeedbackLoop.initialize() must be called first"
            )
