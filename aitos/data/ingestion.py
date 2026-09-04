"""Compatibility facade for the canonical market-data runtime."""

from __future__ import annotations

import asyncio
from typing import Any

from aitos.market_data.binance_adapter import BinanceCanonicalMarketDataAdapter
from aitos.market_data.bus import MarketDataBus
from aitos.market_data.deep_orderbook import DeepOrderBookStore
from aitos.market_data.deep_orderbook_collector import DeepOrderBookCollector
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
LIVE_DEEP_ANCHOR = "BTCUSDT"
LIVE_DEEP_NON_BTC = 2


class DataIngestionService(_LegacyDataIngestionService):
    """Legacy-compatible facade backed by canonical market-data runtimes."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        live_trade_handler = kwargs.get("live_trade_handler")
        live_orderbook_handler = kwargs.get("live_orderbook_handler")
        scanner = next(
            (
                getattr(handler, "__self__", None)
                for handler in (live_trade_handler, live_orderbook_handler)
                if getattr(getattr(handler, "__self__", None), "module_id", None)
                == "opportunity-scanner"
            ),
            None,
        )
        self._canonical_mode = scanner is not None
        if self._canonical_mode:
            kwargs["live_trade_handler"] = None
            kwargs["live_orderbook_handler"] = None
        elif live_trade_handler is None and live_orderbook_handler is None:
            async def _legacy_trade_sink(_trade) -> None:
                return None

            async def _legacy_book_sink(_book) -> None:
                return None

            kwargs["live_trade_handler"] = _legacy_trade_sink
            kwargs["live_orderbook_handler"] = _legacy_book_sink
        super().__init__(*args, **kwargs)
        self._canonical_runtime: CanonicalMarketDataRuntime | None = None
        self._deep_runtime: CanonicalMarketDataRuntime | None = None
        self._canonical_persistence: CanonicalMarketDataPersistenceSink | None = None
        self._deep_collector: DeepOrderBookCollector | None = None
        self._ranking_hook_installed = False
        if self._canonical_mode:
            market_type = str(getattr(self._exchange, "market_type", "usd_m_futures"))
            market_bus = MarketDataBus(self._event_bus)
            gateway = MarketDataGateway(
                venue="binance", market_type=market_type, publisher=market_bus.publish
            )
            initial_orderbooks = [
                LIVE_DEEP_ANCHOR
                if LIVE_DEEP_ANCHOR in {s.upper() for s in self._symbols}
                else self._symbols[0]
            ]
            self._canonical_runtime = CanonicalMarketDataRuntime(
                adapter=BinanceCanonicalMarketDataAdapter(
                    self._exchange, market_type=market_type
                ),
                market_bus=market_bus,
                gateway=gateway,
                symbols=self._symbols,
                orderbook_symbols=initial_orderbooks,
                orderbook_levels=STANDARD_ORDERBOOK_LEVELS,
            )
            deep_adapter = BinanceCanonicalMarketDataAdapter(
                self._exchange, market_type=market_type
            )
            deep_bus = MarketDataBus(self._event_bus)
            deep_gateway = MarketDataGateway(
                venue="binance", market_type=market_type, publisher=deep_bus.publish
            )
            self._deep_runtime = CanonicalMarketDataRuntime(
                adapter=deep_adapter,
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
            if self._repository is not None:
                self._deep_collector = DeepOrderBookCollector(
                    deep_adapter,
                    DeepOrderBookStore(self._repository),
                    symbols=DEEP_HISTORICAL_SYMBOLS,
                )
            self._install_ranking_hook(scanner)

    def _install_ranking_hook(self, scanner: Any) -> None:
        """Bridge scanner ranking to the canonical runtime without coupling modules."""
        if scanner is None or self._ranking_hook_installed:
            return
        original_rank = scanner.rank
        ingestion = self

        async def rank_with_market_promotion(*args: Any, **kwargs: Any):
            ranked = await original_rank(*args, **kwargs)
            await ingestion.update_live_deep_orderbooks(
                [c.symbol for c in ranked[:LIVE_DEEP_NON_BTC]]
            )
            return ranked

        scanner.rank = rank_with_market_promotion
        self._ranking_hook_installed = True

    async def update_live_deep_orderbooks(
        self, ranked_non_btc_symbols: list[str] | tuple[str, ...]
    ) -> bool:
        """Keep BTC plus the two highest-ranked non-BTC symbols on the WS book feed."""
        candidates = [
            s.upper()
            for s in ranked_non_btc_symbols
            if s and s.upper() != LIVE_DEEP_ANCHOR
        ][:LIVE_DEEP_NON_BTC]
        symbols = list(dict.fromkeys([LIVE_DEEP_ANCHOR, *candidates]))
        if LIVE_DEEP_ANCHOR not in {s.upper() for s in self._symbols}:
            symbols = candidates
        if self._canonical_runtime is None:
            return False
        return await self._canonical_runtime.update_orderbook_symbols(symbols)

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
            if self._deep_collector is not None:
                await self._deep_collector.start()

    async def health_check(self):
        status = await super().health_check()
        if self._canonical_runtime is not None:
            status.details["canonical_market_data"] = (
                self._canonical_runtime.gateway.snapshot()
            )
            status.details["live_deep_orderbook_symbols"] = list(
                self._canonical_runtime.orderbook_symbols
            )
        if self._deep_runtime is not None:
            status.details["deep_market_data"] = self._deep_runtime.gateway.snapshot()
        if self._deep_collector is not None:
            status.details["deep_orderbook_collector"] = self._deep_collector.snapshot()
        if self._canonical_persistence is not None:
            status.details["canonical_persistence"] = (
                self._canonical_persistence.snapshot()
            )
        return status

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        if self._deep_collector is not None:
            await self._deep_collector.stop()
        if self._canonical_persistence is not None:
            await self._canonical_persistence.shutdown()
        if self._deep_runtime is not None:
            await self._deep_runtime.stop()
        if self._canonical_runtime is not None:
            await self._canonical_runtime.stop()
        await super().shutdown(grace_period_seconds)


# Apply the cross-cutting instrumentation after the concrete facade exists.
# Keeping this here preserves the historical runtime behavior while allowing
# aitos.data.__init__ to avoid eagerly importing this module (which would
# reintroduce the persistence_sink <-> aitos.data.repository cycle).
from .trade_recovery_guard import install_trade_recovery_guard
from .transport_telemetry import install_transport_telemetry

install_transport_telemetry(DataIngestionService)
install_trade_recovery_guard(DataIngestionService)


__all__ = [
    "DataIngestionService",
    "kline_topic",
    "liquidity_topic",
    "live_state_topic",
    "orderbook_topic",
    "orderflow_topic",
    "trade_topic",
]
