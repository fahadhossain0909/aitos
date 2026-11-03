"""Transport-neutral contracts for AITOS event delivery.

Concrete transports (Redis Streams, in-process test transport, and future
NATS JetStream adapters) implement these structural interfaces. Keep this
module free of infrastructure imports.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from aitos.core.contracts import Event, EventPriority, EventResponse, HealthStatus


EventHandler = Callable[[Event], Awaitable[EventResponse | None]]


@runtime_checkable
class SubscriptionProtocol(Protocol):
    """Handle returned by a transport subscription."""

    def cancel(self) -> None:
        """Stop delivery for this subscription."""


@runtime_checkable
class EventBusProtocol(Protocol):
    """Structural contract consumed by AITOS modules.

    Implementations must preserve per-topic ordering and at-least-once
    delivery for durable subscriptions. start_id="0" means replay from
    the beginning/oldest retained event; start_id="$" means live-only.
    """

    @property
    def module_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    async def initialize(self, config: dict[str, Any]) -> None: ...

    async def health_check(self) -> HealthStatus: ...

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None: ...

    async def publish(
        self, event: Event, priority: EventPriority | None = None
    ) -> None: ...

    async def subscribe(
        self,
        topic: str,
        handler: EventHandler,
        group: str = "default",
        start_id: str = "0",
    ) -> SubscriptionProtocol: ...

    async def request_reply(
        self, event: Event, timeout_ms: float = 5000
    ) -> EventResponse: ...

    async def replay(
        self, topic: str, since: datetime, handler: EventHandler
    ) -> None: ...
