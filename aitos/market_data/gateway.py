"""Canonical market-data gateway boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from .backpressure import BoundedMarketQueue
from .contracts import MarketEvent, MarketSource
from .gateway_health import GatewayHealth


class GatewayState(str, Enum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    queue_capacity: int = 8192
    max_source_age_seconds: float = 15.0
    publish_timeout_seconds: float = 10.0
    backpressure_poll_seconds: float = 0.005


class MarketDataGateway:
    """Transport-independent gateway with explicit lifecycle and backpressure."""

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

    def _validate_event(self, event: MarketEvent) -> bool:
        self.health.record_event()
        age = event.source_age_seconds
        if (
            event.source == MarketSource.WEBSOCKET
            and age > self.config.max_source_age_seconds
        ):
            self.health.record_reject(
                "stale_websocket", f"source age {age:.3f}s exceeded limit"
            )
            self.state = GatewayState.DEGRADED
            return False
        if event.source == MarketSource.REST:
            self.health.degraded = True
            if self.state == GatewayState.CONNECTED:
                self.state = GatewayState.DEGRADED
        return True

    def accept(self, event: MarketEvent) -> bool:
        """Accept synchronously for compatibility; use ``accept_async`` in runtime."""
        if not self._validate_event(event):
            return False
        accepted = self.queue.put_nowait(event)
        if not accepted:
            self.health.record_reject("backpressure", "gateway queue is full")
            self.health.dropped_events += 1
            self.state = GatewayState.DEGRADED
            return False
        self.health.record_accept()
        return True

    async def accept_async(self, event: MarketEvent) -> bool:
        """Accept without dropping when the bounded queue reaches capacity."""
        if not self._validate_event(event):
            return False
        while not self._stopped_for_accept():
            if self.queue.put_nowait(event):
                self.health.record_accept()
                return True
            self.health.backpressure_events += 1
            self.state = GatewayState.DEGRADED
            await asyncio.sleep(self.config.backpressure_poll_seconds)
        return False

    def _stopped_for_accept(self) -> bool:
        return self.state is GatewayState.STOPPED

    async def drain_once(self) -> None:
        event = await self.queue.get()
        try:
            await asyncio.wait_for(
                self._publisher(event), timeout=self.config.publish_timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.health.record_publish_error(str(exc))
            self.state = GatewayState.DEGRADED
            while not self._stopped_for_accept():
                if self.queue.put_nowait(event):
                    break
                self.health.backpressure_events += 1
                await asyncio.sleep(self.config.backpressure_poll_seconds)
            raise
        else:
            self.health.record_publish()
        finally:
            self.queue.task_done()

    def snapshot(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "queue": self.queue.snapshot(),
            "health": self.health.snapshot(),
        }
