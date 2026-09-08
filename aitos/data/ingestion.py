"""Compatibility facade for the canonical market-data runtime."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from aitos.logging_setup import get_logger
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

logger = get_logger("aitos.data.ingestion")

DEEP_HISTORICAL_SYMBOLS = ("BTCUSDT", "LTCUSDT")
DEEP_ORDERBOOK_LEVELS = 1000
LIVE_ORDERBOOK_LEVELS = 1000
LIVE_ORDERBOOK_FALLBACK_LEVELS = 100
LIVE_DEEP_ANCHOR = "BTCUSDT"
LIVE_DEEP_NON_BTC = 2


def _configured_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", name, raw, default)
        return default
    return value if value > 0 else default


def _configured_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    logger.warning("Invalid %s=%r; using default %s", name, raw, default)
    return default


class DataIngestionService(_LegacyDataIngestionService):
    """Legacy-compatible facade backed by bounded canonical market-data runtimes."""

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
        self._deep_history_enabled = _configured_bool(
            "AITOS_ENABLE_DEEP_HISTORY", False
        )
        self._ranking_hook_installed = False
        self._staged_scanner_installed = False
        self._staged_scanner_install_task: asyncio.Task | None = None
        self._live_trade_symbols: list[str] = []
        if self._canonical_mode:
            market_type = str(getattr(self._exchange, "market_type", "usd_m_futures"))
            market_bus = MarketDataBus(self._event_bus)
            gateway = MarketDataGateway(
                venue="binance", market_type=market_type, publisher=market_bus.publish
            )
            normalized_symbols = {s.upper() for s in self._symbols}
            initial_orderbooks = (
                [LIVE_DEEP_ANCHOR]
                if LIVE_DEEP_ANCHOR in normalized_symbols
                else self._symbols[:1]
            )
            initial_trades = (
                [LIVE_DEEP_ANCHOR]
                if LIVE_DEEP_ANCHOR in normalized_symbols
                else self._symbols[:1]
            )
            self._live_trade_symbols = list(initial_trades)
            live_orderbook_levels = _configured_positive_int(
                "AITOS_LIVE_ORDERBOOK_LEVELS", LIVE_ORDERBOOK_LEVELS
            )
            live_orderbook_fallback = _configured_positive_int(
                "AITOS_LIVE_ORDERBOOK_FALLBACK_LEVELS", LIVE_ORDERBOOK_FALLBACK_LEVELS
            )
            self._canonical_runtime = CanonicalMarketDataRuntime(
                adapter=BinanceCanonicalMarketDataAdapter(
                    self._exchange, market_type=market_type
                ),
                market_bus=market_bus,
                gateway=gateway,
                symbols=initial_trades,
                orderbook_symbols=initial_orderbooks,
                orderbook_levels=live_orderbook_levels,
                orderbook_fallback_levels=live_orderbook_fallback,
            )
            self._canonical_persistence = CanonicalMarketDataPersistenceSink(
                self._event_bus,
                self._repository,
                historical_book_symbols=DEEP_HISTORICAL_SYMBOLS,
                book_interval_seconds=1.0,
                historical_trade_symbols=DEEP_HISTORICAL_SYMBOLS,
            )
            if self._deep_history_enabled:
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
                if self._repository is not None:
                    self._deep_collector = DeepOrderBookCollector(
                        deep_adapter,
                        DeepOrderBookStore(self._repository),
                        symbols=DEEP_HISTORICAL_SYMBOLS,
                    )
            self._install_ranking_hook(scanner)

    def _install_ranking_hook(self, scanner: Any) -> None:
        """Install staged scanner orchestration and wire its transport gates."""
        if scanner is None or self._ranking_hook_installed:
            return
        from aitos.intelligence.staged_scanner import install_staged_scan

        ingestion = self

        async def on_stage(symbols: list[str], stage: str) -> None:
            if stage == "TOP_50":
                await ingestion.update_live_trade_symbols(symbols)
            elif stage == "TOP_2":
                await ingestion.update_live_deep_orderbooks(symbols)
            logger.info(
                "scanner market-data stage reached",
                extra={
                    "aitos_extra": {
                        "stage": stage,
                        "symbol_count": len(symbols),
                        "symbols": list(symbols),
                    }
                },
            )

        self._staged_scanner_install_task = asyncio.create_task(
            install_staged_scan(scanner, on_stage),
            name="aitos-install-staged-scanner",
        )
        self._ranking_hook_installed = True

    async def update_live_trade_symbols(
        self, symbols: list[str] | tuple[str, ...]
    ) -> bool:
        """Hot-switch the canonical trade feed to the scanner's Top-50 cohort."""
        candidates = [s.upper() for s in symbols if s]
        if LIVE_DEEP_ANCHOR in {s.upper() for s in self._symbols}:
            candidates = [LIVE_DEEP_ANCHOR, *candidates]
        normalized = list(dict.fromkeys(candidates))
        if self._canonical_runtime is None:
            return False
        if normalized == self._live_trade_symbols:
            return False
        async with self._canonical_runtime._reconfigure_lock:
            previous = list(self._live_trade_symbols)
            self._live_trade_symbols = normalized
            self._canonical_runtime.symbols = normalized
            if self._canonical_runtime._stopped:
                return True
            trade_tasks = [
                task
                for task in self._canonical_runtime._tasks
                if task.get_name() == "market-data-trades"
            ]
            for task in trade_tasks:
                task.cancel()
            if trade_tasks:
                await asyncio.gather(*trade_tasks, return_exceptions=True)
            self._canonical_runtime._tasks = [
                task
                for task in self._canonical_runtime._tasks
                if task not in trade_tasks
            ]
            if self._canonical_runtime.enable_trades and normalized:
                self._canonical_runtime._tasks.append(
                    asyncio.create_task(
                        self._canonical_runtime._run(
                            "trades",
                            lambda: self._canonical_runtime.adapter.stream_trades(
                                normalized
                            ),
                        ),
                        name="market-data-trades",
                    )
                )
            logger.info(
                "live trade subscription reconfigured",
                extra={
                    "aitos_extra": {
                        "stage": "trade_subscription_reconfigured",
                        "previous_symbols": previous,
                        "trade_symbols": normalized,
                    }
                },
            )
            return True

    async def update_live_deep_orderbooks(
        self, ranked_non_btc_symbols: list[str] | tuple[str, ...]
    ) -> bool:
        """Keep BTC plus the two highest-ranked non-BTC symbols on live book feed."""
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
        # The legacy base class always starts a kline websocket for the full
        # configured universe. Canonical mode deliberately does NOT use that
        # feed: staged scanning obtains cheap klines via REST and canonical
        # live sockets are reserved for the bounded trade/order-book cohorts.
        # Suppress the legacy kline task before calling the base initializer so
        # the broad websocket is never opened in canonical mode.
        legacy_symbols = self._symbols
        if self._canonical_mode:
            self._symbols = []
        try:
            await super().initialize(config)
        finally:
            self._symbols = legacy_symbols
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
            canonical = self._canonical_runtime.gateway.snapshot()
            canonical_health = canonical["health"]
            status.details["canonical_market_data"] = canonical
            status.details["trade_stream_messages_received"] = canonical_health[
                "received_events"
            ]
            status.details["trade_events_received"] = canonical_health[
                "accepted_events"
            ]
            status.details["trade_stream_restarts"] = canonical_health[
                "reconnect_count"
            ]
            status.details["trade_stream_idle_timeouts"] = canonical_health[
                "stream_idle_timeouts"
            ]
            status.details["trade_parse_errors"] = canonical_health["decode_errors"]
            status.details["trade_stream_errors"] = canonical_health["reconnect_count"]
            status.details["trade_downstream_errors"] = canonical_health[
                "publish_errors"
            ]
            status.details["trade_stream_dropped"] = canonical_health["dropped_events"]
            status.details["trade_freshness_drops"] = canonical_health[
                "freshness_drops"
            ]
            status.details["last_trade_event_time"] = canonical_health.get(
                "last_event_at"
            )
            status.details["canonical_trade_health_source"] = (
                "market_data.gateway.health"
            )
            status.details["live_trade_symbols"] = list(self._live_trade_symbols)
            status.details["live_deep_orderbook_symbols"] = list(
                self._canonical_runtime.orderbook_symbols
            )
            status.details["live_orderbook_levels"] = (
                self._canonical_runtime.orderbook_levels
            )
            status.details["live_orderbook_fallback_levels"] = (
                self._canonical_runtime.orderbook_fallback_levels
            )
        status.details["deep_history_enabled"] = self._deep_history_enabled
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
        if self._staged_scanner_install_task is not None:
            self._staged_scanner_install_task.cancel()
            await asyncio.gather(
                self._staged_scanner_install_task, return_exceptions=True
            )
            self._staged_scanner_install_task = None
        if self._deep_collector is not None:
            await self._deep_collector.stop()
        if self._canonical_persistence is not None:
            await self._canonical_persistence.shutdown()
        if self._deep_runtime is not None:
            await self._deep_runtime.stop()
        if self._canonical_runtime is not None:
            await self._canonical_runtime.stop()
        await super().shutdown(grace_period_seconds)


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
