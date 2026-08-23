"""MLExplainerFeedbackLoop — trains ``TradeOutcomeClassifier`` online from
real closed trades, the same Event-Bus-subscription pattern as
``JournalSystem`` and ``RLFeedbackLoop``. Once enough trades have closed,
``classifier.is_ready`` flips True and ``explain()`` starts returning real
SHAP values instead of an empty dict.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

from aitos.core.contracts import (AITOSModule, Event, EventResponse,
                                  HealthStatus, ModuleStatus)
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.logging_setup import get_logger
from aitos.xai.ml_explainer import TradeOutcomeClassifier

logger = get_logger("aitos.xai.ml_feedback")


class MLExplainerFeedbackLoop(AITOSModule):
    def __init__(self, event_bus: EventBus, classifier: TradeOutcomeClassifier) -> None:
        self._event_bus = event_bus
        self._classifier = classifier
        self._initialized = False
        self._subscriptions: List[Subscription] = []
        self._updates_applied = 0
        self._last_event_time: Optional[str] = None

    @property
    def module_id(self) -> str:
        return "ml-explainer-feedback-loop"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: Dict[str, Any]) -> None:
        if self._initialized:
            return
        self._subscriptions.append(
            await self._event_bus.subscribe(
                "trade.position_closed",
                self._on_position_closed,
                group="ml-explainer-feedback",
            )
        )
        self._initialized = True
        logger.info("MLExplainerFeedbackLoop initialized")

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
                "classifier_ready": self._classifier.is_ready,
                "samples_seen": self._classifier.n_samples_seen,
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        for sub in self._subscriptions:
            sub.cancel()
        self._subscriptions.clear()
        logger.info("MLExplainerFeedbackLoop shut down")

    async def emit_events(self) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> Optional[EventResponse]:
        return None

    @property
    def updates_applied(self) -> int:
        return self._updates_applied

    async def _on_position_closed(self, event: Event) -> Optional[EventResponse]:
        trade_dict = event.payload
        pnl = trade_dict.get("pnl")
        agent_consensus = trade_dict.get("agent_consensus")
        if pnl is None or not agent_consensus:
            return None  # can't train without both a labeled outcome and the feature vector that produced it

        self._classifier.partial_fit(agent_consensus, won=pnl > 0)
        self._updates_applied += 1
        self._last_event_time = event.created_at
        logger.info(
            "outcome classifier updated from closed trade",
            extra={
                "aitos_extra": {
                    "won": pnl > 0,
                    "samples_seen": self._classifier.n_samples_seen,
                    "is_ready": self._classifier.is_ready,
                }
            },
        )
        return None

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "MLExplainerFeedbackLoop.initialize() must be called first"
            )
