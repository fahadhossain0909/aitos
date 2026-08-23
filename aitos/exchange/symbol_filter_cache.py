"""Periodic exchange-info refresh for live Binance symbol filters."""

from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from aitos.exchange.binance import BinanceFuturesAdapter
from aitos.exchange.symbol_filters import SymbolFilters
from aitos.execution.binance_executor import BinanceFuturesOrderExecutor
from aitos.logging_setup import get_logger

logger = get_logger("aitos.exchange.symbol_filter_cache")


class SymbolFilterCacheRefresher:
    """Keep executor precision data synchronized with Binance exchangeInfo."""

    def __init__(
        self,
        executor: BinanceFuturesOrderExecutor,
        symbols: List[str],
        ttl_seconds: float = 24 * 60 * 60,
    ) -> None:
        self._executor = executor
        self._symbols = list(symbols)
        self._ttl_seconds = ttl_seconds
        self._adapter = BinanceFuturesAdapter()
        self._task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        await self._refresh()
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="binance-symbol-filter-refresh")

    async def _refresh(self) -> Dict[str, SymbolFilters]:
        try:
            async with self._adapter:
                filters = await self._adapter.fetch_exchange_info(
                    symbols=self._symbols
                )
            self._executor.load_symbol_filters(filters)
            logger.info(
                "refreshed Binance exchangeInfo precision",
                extra={
                    "aitos_extra": {
                        "symbols": list(filters.keys()),
                        "ttl_seconds": self._ttl_seconds,
                    }
                },
            )
            return filters
        except Exception as exc:
            logger.warning("Binance exchangeInfo refresh failed: %s", exc)
            return {}

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self._ttl_seconds
                )
            except asyncio.TimeoutError:
                await self._refresh()

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._adapter.close()
