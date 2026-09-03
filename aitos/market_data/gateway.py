"""Canonical market-data gateway boundary.

The gateway owns transport lifecycle and turns normalized events into a bounded
publish path. Exchange-specific WebSocket code belongs in adapters; this class
must not know Binance payload shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable

from .backpressure import BoundedMarketQueue
from .contracts import MarketEvent
from .gateway_health import GatewayHealth


class GatewayState(str, Enum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    queue_capacity: int = 2048
    max_source_age_seconds: float = 15.0


class MarketDataGateway:
    """Transport-independent gateway with explicit lifecycle state."""

    def __init__(
        self,
        venue: str,
        market_type: str,
        publisher: Callable[[MarketEvent], Awaitable[None]],
        config: GatewayConfig | None = None,
    ) -> None:
        self.config = config or GatewayConfig()
        self.queue = BoundedMarketQueue[MarketEvent](self.config.queue_capacity)
        self.health = GatewayHealth(venue, market_type)
        self.state = GatewayState.STOPPED
        self._publisher = publisher

    def begin_connect(self) -> None:
        self.state = GatewayState.CONNECTING
        self.health.connected = False

    def mark_connected(self) -> None:
        self.state = GatewayState.CONNECTED
        self.health.connected = True
        self.health.degraded = False

    def mark_reconnecting(self) -> None:
        self.state = GatewayState.RECONNECTING
        self.health.reconnect()

    def stop(self) -> None:
        self.state = GatewayState.STOPPED
        self.health.connected = False

    def accept(self, event: MarketEvent) -> bool:
        """Accept an already-normalized event without blocking the transport."""
        self.health.record_event()
        if event.source_age_seconds > self.config.max_source_age_seconds:
            self.health.record_error(
                "stale", f"source age {event.source_age_seconds:.3f}s exceeded limit"
            )
            return False
        accepted = self.queue.put_nowait(event)
        if not accepted:
            self.health.dropped_events += 1
            self.state = GatewayState.DEGRADED
        return accepted

    async def drain_once(self) -> None:
        event = await self.queue.get()
        try:
            await self._publisher(event)
            self.health.record_publish()
        finally:
            self.queue.task_done()

    def snapshot(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "queue": self.queue.snapshot(),
            "health": self.health.snapshot(),
        }
