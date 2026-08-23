"""RL feedback loop for continuously learning trade outcomes."""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional, Union

from aitos.core.contracts import (AITOSModule, Event, EventResponse,
                                  HealthStatus, ModuleStatus)
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.intelligence.deep_rl_policy import DeepValueRLScorer
from aitos.intelligence.rl_policy import TabularBanditRLScorer
from aitos.logging_setup import get_logger

logger = get_logger("aitos.intelligence.rl_feedback")
TrainableRLScorer = Union[TabularBanditRLScorer, DeepValueRLScorer]


class RLFeedbackLoop(AITOSModule):
    def __init__(self, event_bus: EventBus, scorer: TrainableRLScorer) -> None:
        self._event_bus = event_bus
        self._scorer = scorer
        self._initialized = False
        self._subscriptions: List[Subscription] = []
        self._updates_applied = 0
        self._last_event_time: Optional[str] = None

    @property
    def module_id(self) -> str:
        return "rl-feedback-loop"

    @property
    def version(self) -> str:
        return "1.1.0"

    async def initialize(self, config: Dict[str, Any]) -> None:
        if self._initialized:
            return
        self._subscriptions.append(
            await self._event_bus.subscribe(
                "trade.position_closed", self._on_position_closed, group="rl-feedback"
            )
        )
        self._initialized = True
        logger.info("RLFeedbackLoop initialized")

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            module_id=self.module_id,
            status=(
                ModuleStatus.HEALTHY if self._initialized else ModuleStatus.UNHEALTHY
            ),
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={"updates_applied": self._updates_applied},
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        for sub in self._subscriptions:
            sub.cancel()
        self._subscriptions.clear()

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
        risk_amount = trade_dict.get("risk_amount_usd") or 0.0
        pnl = trade_dict.get("pnl")
        if not risk_amount or pnl is None:
            return None

        reward_r_multiple = pnl / risk_amount
        symbol = trade_dict.get("symbol", "unknown")
        regime = trade_dict.get("regime", "unknown")
        direction = trade_dict.get("side", "unknown")
        agent_consensus = trade_dict.get("agent_consensus") or {}
        context = {"regime": regime, "direction": direction, **agent_consensus}

        if isinstance(self._scorer, DeepValueRLScorer):
            # Paper/live and the historical-learning worker share one durable
            # checkpoint. Use the scorer's atomic read-modify-write path so a
            # concurrent update cannot overwrite another process's learning.
            self._scorer.update_and_persist(symbol, context, reward_r_multiple)
        else:
            self._scorer.update(
                symbol=symbol, context=context, reward_r_multiple=reward_r_multiple
            )

        self._updates_applied += 1
        self._last_event_time = event.created_at
        logger.info(
            "RL scorer updated from closed trade",
            extra={
                "aitos_extra": {
                    "symbol": symbol,
                    "regime": regime,
                    "direction": direction,
                    "reward_r_multiple": reward_r_multiple,
                }
            },
        )
        return None

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "RLFeedbackLoop.initialize() must be called first"
            )
