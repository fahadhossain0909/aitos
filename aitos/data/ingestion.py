"""Compatibility facade for the canonical market-data runtime."""

from __future__ import annotations

import asyncio
from typing import Any

from aitos.market_data.binance_adapter import BinanceCanonicalMarketDataAdapter
from aitos.market_data.bus import MarketDataBus
from aitos.market_data.gateway import MarketDataGateway
from aitos.market_data.persistence_sink import CanonicalMarketDataPersistenceSink
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

DEEP_HISTORICAL_SYMBOLS = ("BTCUSDT", "LTCUSDT")
DEEP_ORDERBOOK_LEVELS = 1000
STANDARD_ORDERBOOK_LEVELS = 100


class DataIngestionService(_LegacyDataIngestionService):
    """Legacy-compatible facade backed by canonical market-data runtimes."""

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
        self._deep_runtime: CanonicalMarketDataRuntime | None = None
        self._canonical_persistence: CanonicalMarketDataPersistenceSink | None = None
        if self._canonical_mode:
            market_type = str(getattr(self._exchange, "market_type", "usd_m_futures"))
            market_bus = MarketDataBus(self._event_bus)
            gateway = MarketDataGateway(
                venue="binance", market_type=market_type, publisher=market_bus.publish
            )
            deep = set(DEEP_HISTORICAL_SYMBOLS)
            standard_orderbooks = [
                symbol for symbol in self._symbols if symbol.upper() not in deep
            ]
            self._canonical_runtime = CanonicalMarketDataRuntime(
                adapter=BinanceCanonicalMarketDataAdapter(
                    self._exchange, market_type=market_type
                ),
                market_bus=market_bus,
                gateway=gateway,
                symbols=self._symbols,
                orderbook_symbols=standard_orderbooks,
                orderbook_levels=STANDARD_ORDERBOOK_LEVELS,
            )
            deep_bus = MarketDataBus(self._event_bus)
            deep_gateway = MarketDataGateway(
                venue="binance", market_type=market_type, publisher=deep_bus.publish
            )
            self._deep_runtime = CanonicalMarketDataRuntime(
                adapter=BinanceCanonicalMarketDataAdapter(
                    self._exchange, market_type=market_type
                ),
                market_bus=deep_bus,
                gateway=deep_gateway,
                symbols=[],
                orderbook_symbols=list(DEEP_HISTORICAL_SYMBOLS),
                orderbook_levels=DEEP_ORDERBOOK_LEVELS,
                enable_trades=False,
                enable_orderbooks=True,
            )
            self._canonical_persistence = CanonicalMarketDataPersistenceSink(
                self._event_bus,
                self._repository,
                historical_book_symbols=DEEP_HISTORICAL_SYMBOLS,
                book_interval_seconds=1.0,
            )

    async def initialize(self, config: dict[str, Any]) -> None:
        await super().initialize(config)
        if self._canonical_runtime is not None:
            legacy_workers = [
                task
                for task in self._tasks
                if task.get_name().startswith("aitos-trade-persistence-")
            ]
            for task in legacy_workers:
                task.cancel()
            if legacy_workers:
                await asyncio.gather(*legacy_workers, return_exceptions=True)
            self._tasks = [task for task in self._tasks if task not in legacy_workers]
            if self._canonical_persistence is not None:
                await self._canonical_persistence.initialize()
            await self._canonical_runtime.start()
            if self._deep_runtime is not None:
                await self._deep_runtime.start()

    async def health_check(self):
        status = await super().health_check()
        if self._canonical_runtime is not None:
            status.details["canonical_market_data"] = (
                self._canonical_runtime.gateway.snapshot()
            )
        if self._deep_runtime is not None:
            status.details["deep_market_data"] = self._deep_runtime.gateway.snapshot()
        if self._canonical_persistence is not None:
            status.details["canonical_persistence"] = (
                self._canonical_persistence.snapshot()
            )
        return status

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        if self._canonical_persistence is not None:
            await self._canonical_persistence.shutdown()
        if self._deep_runtime is not None:
            await self._deep_runtime.stop()
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
