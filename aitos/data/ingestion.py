"""Compatibility facade for the canonical market-data runtime."""

from __future__ import annotations

from typing import Any

from aitos.market_data.binance_adapter import BinanceCanonicalMarketDataAdapter
from aitos.market_data.bus import MarketDataBus
from aitos.market_data.gateway import MarketDataGateway
from aitos.market_data.runtime import CanonicalMarketDataRuntime

from .ingestion_legacy import DataIngestionService as _LegacyDataIngestionService
from .ingestion_legacy import (
    kline_topic,
    liquidity_topic,
    live_state_topic,
    orderbook_topic,
    orderflow_topic,
    trade_topic,
)


class DataIngestionService(_LegacyDataIngestionService):
    """Legacy-compatible facade that delegates live transport to MarketData V1.

    The application historically supplied scanner callbacks to this service.
    Those callbacks are recognized as the old direct path and are intentionally
    disabled here; the scanner now consumes canonical semantic channels.
    Explicit non-scanner handlers remain supported for focused compatibility
    tests and legacy callers.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        live_trade_handler = kwargs.get("live_trade_handler")
        live_orderbook_handler = kwargs.get("live_orderbook_handler")
        self._canonical_mode = any(
            getattr(getattr(handler, "__self__", None), "module_id", None)
            == "opportunity-scanner"
            for handler in (live_trade_handler, live_orderbook_handler)
            if handler is not None
        )
        if self._canonical_mode:
            kwargs["live_trade_handler"] = None
            kwargs["live_orderbook_handler"] = None
        super().__init__(*args, **kwargs)
        self._canonical_runtime: CanonicalMarketDataRuntime | None = None
        if self._canonical_mode:
            exchange = self._exchange
            market_type = str(getattr(exchange, "market_type", "usd_m_futures"))
            market_bus = MarketDataBus(self._event_bus)
            gateway = MarketDataGateway(
                venue="binance",
                market_type=market_type,
                publisher=market_bus.publish,
            )
            self._canonical_runtime = CanonicalMarketDataRuntime(
                adapter=BinanceCanonicalMarketDataAdapter(
                    exchange, market_type=market_type
                ),
                market_bus=market_bus,
                gateway=gateway,
                symbols=self._symbols,
                orderbook_levels=self._orderbook_levels,
            )

    async def initialize(self, config: dict[str, Any]) -> None:
        await super().initialize(config)
        if self._canonical_runtime is not None:
            await self._canonical_runtime.start()

    async def health_check(self):
        status = await super().health_check()
        if self._canonical_runtime is not None:
            status.details["canonical_market_data"] = (
                self._canonical_runtime.gateway.snapshot()
            )
        return status

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        if self._canonical_runtime is not None:
            await self._canonical_runtime.stop()
        await super().shutdown(grace_period_seconds)


__all__ = [
    "DataIngestionService",
    "kline_topic",
    "liquidity_topic",
    "live_state_topic",
    "orderbook_topic",
    "orderflow_topic",
    "trade_topic",
]
