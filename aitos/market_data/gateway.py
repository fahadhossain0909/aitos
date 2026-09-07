"""Canonical market-data gateway boundary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

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
    max_queue_age_seconds: float = 1.0
    publish_timeout_seconds: float = 1.0
    backpressure_poll_seconds: float = 0.005


class MarketDataGateway:
    """Transport-independent gateway with freshness-first backpressure."""

    def __init__(
        self,
        venue: str,
        market_type: str,
        publisher: Any,
        config: GatewayConfig | None = None,
        transport_snapshot_provider: Any | None = None,
    ) -> None:
        self.config = config or GatewayConfig()
        self.queue = BoundedMarketQueue[MarketEvent](self.config.queue_capacity)
        self.health = GatewayHealth(venue, market_type)
        self.state = GatewayState.STOPPED
        self._publisher = publisher
        self._transport_snapshot_provider = transport_snapshot_provider

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

    def _accept_latest(self, event: MarketEvent) -> bool:
        replaced_before = self.queue.stats.replaced_oldest
        if self.queue.put_latest_nowait(event):
            self.health.record_accept()
            if self.queue.stats.replaced_oldest > replaced_before:
                self.health.backpressure_events += 1
            return True
        self.health.record_freshness_drop("unable to enqueue newest market event")
        return False

    def accept(self, event: MarketEvent) -> bool:
        """Accept synchronously without allowing an old queue to stall live data."""
        if not self._validate_event(event):
            return False
        return self._accept_latest(event)

    async def accept_async(self, event: MarketEvent) -> bool:
        """Accept immediately; replace old queued data instead of waiting."""
        if not self._validate_event(event):
            return False
        return self._accept_latest(event)

    def _stopped_for_accept(self) -> bool:
        return self.state is GatewayState.STOPPED

    @staticmethod
    def _queue_age_seconds(event: MarketEvent) -> float:
        return max(0.0, (datetime.now(timezone.utc) - event.ingest_time).total_seconds())

    async def drain_once(self) -> None:
        event = await self.queue.get()
        try:
            queue_age = self._queue_age_seconds(event)
            if queue_age > self.config.max_queue_age_seconds:
                self.health.record_freshness_drop(
                    f"queued market event age {queue_age:.3f}s exceeded "
                    f"{self.config.max_queue_age_seconds:.3f}s"
                )
                return
            try:
                await asyncio.wait_for(
                    self._publisher(event), timeout=self.config.publish_timeout_seconds
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never requeue an old failed event: doing so can create a stale
                # backlog and starve newer market data. The next fresh event is
                # allowed to proceed immediately.
                self.health.record_publish_error(str(exc))
                self.health.dropped_events += 1
                self.state = GatewayState.DEGRADED
                raise
            else:
                self.health.record_publish()
        finally:
            self.queue.task_done()

    def snapshot(self) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "state": self.state.value,
            "queue": self.queue.snapshot(),
            "health": self.health.snapshot(),
            "freshness_policy": {
                "max_queue_age_seconds": self.config.max_queue_age_seconds,
                "publish_timeout_seconds": self.config.publish_timeout_seconds,
                "overflow": "replace_oldest_with_newest",
                "failed_publish": "drop_failed_event",
            },
        }
        if self._transport_snapshot_provider is not None:
            try:
                snapshot["transport"] = self._transport_snapshot_provider()
            except Exception as exc:
                snapshot["transport"] = {
                    "state": "telemetry_error",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
        return snapshot
