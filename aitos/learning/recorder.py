"""Event-driven persistence of paper/live decisions and outcomes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.data.repository import MarketDataRepository
from aitos.eventbus.redis_bus import EventBus, Subscription

from .experience import ExperienceRecord


def _event_time(event: Event) -> datetime:
    value = datetime.fromisoformat(event.created_at.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class LearningExperienceRecorder(AITOSModule):
    """Turn decision/outcome events into durable learning experiences."""

    def __init__(
        self,
        event_bus: EventBus,
        repository: MarketDataRepository | None,
        source: str,
    ) -> None:
        if source not in {"paper", "live"}:
            raise ValueError("source must be paper or live")
        self._event_bus = event_bus
        self._repository = repository
        self._source = source
        self._subscriptions: list[Subscription] = []
        self._initialized = False
        self._records_written = 0
        self._decision_events_received = 0
        self._outcome_events_received = 0
        self._last_event_time: str | None = None

    @property
    def module_id(self) -> str:
        return f"learning-experience-recorder-{self._source}"

    @property
    def version(self) -> str:
        return "1.1.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        if self._repository is not None:
            await self._repository.ensure_learning_experience_schema()
        self._subscriptions.extend(
            [
                await self._event_bus.subscribe(
                    "journal.decision_recorded",
                    self._on_decision,
                    group=f"learning-{self._source}",
                ),
                await self._event_bus.subscribe(
                    "journal.outcome_attributed",
                    self._on_outcome,
                    group=f"learning-{self._source}",
                ),
            ]
        )
        self._initialized = True

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            module_id=self.module_id,
            status=(
                ModuleStatus.HEALTHY if self._initialized else ModuleStatus.UNHEALTHY
            ),
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={
                "records_written": self._records_written,
                "decision_events_received": self._decision_events_received,
                "outcome_events_received": self._outcome_events_received,
                "source": self._source,
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        for sub in self._subscriptions:
            sub.cancel()
        self._subscriptions.clear()

    async def emit_events(self) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    async def _write(self, record: ExperienceRecord) -> None:
        if self._repository is not None:
            await self._repository.save_learning_experience(record)
            self._records_written += 1

    async def _on_decision(self, event: Event) -> EventResponse | None:
        self._decision_events_received += 1
        p = dict(event.payload)
        record = ExperienceRecord(
            timestamp=_event_time(event),
            source=self._source,
            symbol=str(p.get("symbol", "unknown")),
            decision=str(p.get("side", p.get("direction", "unknown"))),
            confidence=float(p.get("confidence", 0.0) or 0.0),
            quantity=float(p.get("quantity", p.get("position_size", 0.0)) or 0.0),
            price=float(p["entry_price"]) if p.get("entry_price") is not None else None,
            features=dict(p.get("agent_consensus") or {}),
            market_state={
                "regime": p.get("regime"),
                "kernel_direction": p.get("kernel_direction"),
                "kernel_confidence": p.get("kernel_confidence"),
            },
            risk_state=dict(p.get("risk_state") or {}),
            strategy_version=str(p.get("strategy_id", "unknown")),
            model_version=str(p.get("model_version", "unknown")),
            metadata={
                "decision_id": str(p.get("decision_id") or event.event_id),
                "event_type": "decision",
            },
        )
        await self._write(record)
        self._last_event_time = event.created_at
        return None

    async def _on_outcome(self, event: Event) -> EventResponse | None:
        self._outcome_events_received += 1
        p = dict(event.payload)
        pnl = p.get("pnl")
        if pnl is None:
            return None
        record = ExperienceRecord(
            timestamp=_event_time(event),
            source=self._source,
            symbol=str(p.get("symbol", "unknown")),
            decision=str(p.get("side", "outcome")),
            outcome="closed",
            reward=float(pnl),
            metadata={
                "decision_id": str(p.get("decision_id", "")),
                "trade_id": str(p.get("trade_id", "")),
                "event_type": "outcome",
            },
        )
        await self._write(record)
        self._last_event_time = event.created_at
        return None
