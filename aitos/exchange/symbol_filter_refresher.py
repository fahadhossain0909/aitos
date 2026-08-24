"""TTL-based exchange-info refresh for live order precision safety."""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional

from aitos.logging_setup import get_logger

logger = get_logger("aitos.exchange.symbol_filter_refresher")

DEFAULT_SYMBOL_FILTER_TTL_SECONDS = 300.0


class SymbolFilterRefresher:
    """Refresh Binance symbol filters periodically without blocking trading."""

    def __init__(
        self,
        exchange: Any,
        executor: Any,
        symbols: List[str],
        ttl_seconds: float = DEFAULT_SYMBOL_FILTER_TTL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._exchange = exchange
        self._executor = executor
        self._symbols = list(symbols)
        self._ttl_seconds = ttl_seconds
        self._task: Optional[asyncio.Task] = None
        self._last_refresh_at: Optional[float] = None
        self._refresh_count = 0
        self._last_error: Optional[str] = None

    async def refresh_now(self) -> None:
        filters = await self._exchange.fetch_exchange_info(symbols=self._symbols)
        if not filters:
            raise RuntimeError(
                "exchangeInfo returned no filters for configured symbols"
            )
        self._executor.load_symbol_filters(filters)
        self._last_refresh_at = asyncio.get_running_loop().time()
        self._refresh_count += 1
        self._last_error = None
        logger.info(
            "refreshed exchange symbol filters",
            extra={
                "aitos_extra": {
                    "symbols": list(filters),
                    "refresh_count": self._refresh_count,
                }
            },
        )

    async def start(self) -> None:
        await self.refresh_now()
        self._task = asyncio.create_task(
            self._run(), name="aitos-symbol-filter-refresh"
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._ttl_seconds)
            try:
                await self.refresh_now()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                logger.error(
                    "exchange symbol-filter refresh failed; retaining last known-good filters",
                    extra={"aitos_extra": {"error": str(exc)}},
                )

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def refresh_count(self) -> int:
        return self._refresh_count
