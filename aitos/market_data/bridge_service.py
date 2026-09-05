"""Temporary one-way bridge from legacy ingestion into canonical market data."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.logging_setup import get_logger
from aitos.models.market import Kline, OrderBookSnapshot, TradeTick

from .bus import MarketDataBus
from .contracts import MarketSource
from .legacy_bridge import book_snapshot_event, kline_event, trade_event

logger = get_logger("aitos.market_data.bridge")


class LegacyMarketDataBridge(AITOSModule):
    """Migration seam; it is deliberately not a second exchange connection."""

    def __init__(self, event_bus: EventBus, market_bus: MarketDataBus) -> None:
        self._event_bus = event_bus
        self._market_bus = market_bus
        self._subscriptions: list[Subscription] = []
        self._initialized = False
        self._published = 0
        self._errors = 0

    @property
    def module_id(self) -> str:
        return "market-data-canonical-bridge"

    @property
    def version(self) -> str:
        return "1.1.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        # Legacy EventBus has no wildcard Streams subscription. The migration
        # bridge therefore subscribes only to the configured concrete symbols.
        symbols = [str(s).upper() for s in config.get("symbols", [])]
        if not symbols:
            logger.warning("canonical bridge disabled: no legacy symbols configured")
            self._initialized = True
            return
        self._subscriptions = []
        for symbol in symbols:
            self._subscriptions.extend(
                [
                    await self._event_bus.subscribe(
                        f"market.trade.{symbol}",
                        self._on_trade,
                        group="market-data-bridge",
                        start_id="$",
                    ),
                    await self._event_bus.subscribe(
                        f"market.orderbook.{symbol}",
                        self._on_book,
                        group="market-data-bridge",
                        start_id="$",
                    ),
                    await self._event_bus.subscribe(
                        f"market.kline.{symbol}.*",
                        self._on_kline,
                        group="market-data-bridge",
                        start_id="$",
                    ),
                ]
            )
        self._initialized = True
        logger.info(
            "canonical market-data bridge initialized",
            extra={"aitos_extra": {"symbols": symbols}},
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        if self._subscriptions:
            await asyncio.gather(
                *(self._wait_subscription(s) for s in self._subscriptions),
                return_exceptions=True,
            )
        self._subscriptions.clear()
        self._initialized = False

    async def _wait_subscription(self, subscription: Subscription) -> None:
        try:
            await subscription._task
        except asyncio.CancelledError:
            pass

    async def health_check(self) -> HealthStatus:
        status = (
            ModuleStatus.HEALTHY
            if self._initialized and not self._errors
            else ModuleStatus.DEGRADED
        )
        return HealthStatus(
            module_id=self.module_id,
            status=status,
            latency_ms=0.0,
            details={
                "published": self._published,
                "errors": self._errors,
                "subscriptions": len(self._subscriptions),
            },
        )

    async def emit_events(self) -> AsyncIterator[Event]:
        return
        yield

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    @staticmethod
    def _source(event: Event) -> MarketSource:
        raw = (
            event.payload.get("_market_source")
            if isinstance(event.payload, dict)
            else None
        )
        try:
            return MarketSource(str(raw)) if raw else MarketSource.WEBSOCKET
        except ValueError:
            return MarketSource.WEBSOCKET

    async def _publish(self, market_event) -> None:
        await self._market_bus.publish(market_event)
        self._published += 1

    async def _on_trade(self, event: Event) -> None:
        try:
            await self._publish(
                trade_event(
                    TradeTick.from_dict(event.payload), source=self._source(event)
                )
            )
        except Exception:
            self._errors += 1
            logger.exception(
                "failed to bridge trade event",
                extra={"aitos_extra": {"topic": event.topic}},
            )

    async def _on_book(self, event: Event) -> None:
        try:
            await self._publish(
                book_snapshot_event(
                    OrderBookSnapshot.from_dict(event.payload),
                    source=self._source(event),
                )
            )
        except Exception:
            self._errors += 1
            logger.exception(
                "failed to bridge order-book event",
                extra={"aitos_extra": {"topic": event.topic}},
            )

    async def _on_kline(self, event: Event) -> None:
        try:
            await self._publish(
                kline_event(Kline.from_dict(event.payload), source=self._source(event))
            )
        except Exception:
            self._errors += 1
            logger.exception(
                "failed to bridge kline event",
                extra={"aitos_extra": {"topic": event.topic}},
            )
