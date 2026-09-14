"""REST fallback when live websocket prices go quiet for open positions.

Hard SL is only evaluated inside ``TradeLifecycle.update_price()``, which is
driven exclusively by live market events. If the websocket path stops
delivering prices for a symbol that has an open position, the position is
effectively unprotected until the live path recovers.

This module polls open-position symbols on a short interval, and for any
that have not received a live update within ``stale_after_seconds`` it
fetches a REST last-trade price and routes it through ``update_price()`` so
the authoritative hard-SL path still runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Protocol

from aitos.logging_setup import get_logger
from aitos.trading.price_freshness import (
    PriceFreshnessTracker,
    get_global_tracker,
)

logger = get_logger("aitos.trading.price_safety_net")

DEFAULT_STALE_AFTER_SECONDS = 20.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0


class _ExchangeLike(Protocol):
    async def fetch_recent_trades(
        self, symbol: str, limit: int = 500
    ) -> Sequence[Any]: ...


class _LifecycleLike(Protocol):
    def get_open_trades(self) -> Sequence[Any]: ...

    async def update_price(
        self, trade_id: str, current_price: float, **kwargs: Any
    ) -> Any: ...


class PositionPriceSafetyNet:
    """Background poller that REST-rescues stale open-position symbols."""

    def __init__(
        self,
        exchange: _ExchangeLike,
        lifecycles_provider: Callable[[], Iterable[_LifecycleLike]],
        *,
        freshness: PriceFreshnessTracker | None = None,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._exchange = exchange
        self._lifecycles_provider = lifecycles_provider
        self._freshness = freshness or get_global_tracker()
        self._stale_after = float(stale_after_seconds)
        self._interval = float(poll_interval_seconds)
        self._task: asyncio.Task | None = None
        self._stopped = False
        self.polls_run = 0
        self.symbols_rescued = 0

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(
            self._run(), name="aitos-position-price-safety-net"
        )

    async def stop(self) -> None:
        self._stopped = True
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("position price safety net poll failed")
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise

    async def poll_once(self) -> int:
        """Check every open-position symbol for live-price staleness; for
        any that are stale, fetch a REST price and route it through
        ``update_price()``. Returns how many symbols were rescued.
        """
        self.polls_run += 1
        stale: dict[str, list[tuple[Any, Any]]] = {}
        for lifecycle in list(self._lifecycles_provider()):
            try:
                trades = list(lifecycle.get_open_trades())
            except Exception:
                continue
            for trade in trades:
                symbol = str(getattr(trade, "symbol", "") or "").upper()
                if not symbol:
                    continue
                if self._freshness.is_stale(symbol, self._stale_after):
                    stale.setdefault(symbol, []).append((lifecycle, trade))

        rescued = 0
        for symbol, pairs in stale.items():
            try:
                price = await self._fetch_last_price(symbol)
            except Exception as exc:
                logger.warning(
                    "safety-net REST price fetch failed",
                    extra={"aitos_extra": {"symbol": symbol, "error": str(exc)}},
                )
                continue
            if price is None:
                continue
            for lifecycle, trade in pairs:
                try:
                    await lifecycle.update_price(trade.trade_id, price)
                except Exception:
                    logger.exception(
                        "safety-net update_price failed",
                        extra={
                            "aitos_extra": {
                                "symbol": symbol,
                                "trade_id": getattr(trade, "trade_id", None),
                            }
                        },
                    )
            # Mark seen so we don't hammer REST every interval while the
            # websocket stays down; the live path overwrites this the
            # instant it recovers for that symbol.
            self._freshness.note_seen(symbol)
            rescued += 1
            logger.warning(
                "position price safety net used REST fallback for a stale symbol",
                extra={
                    "aitos_extra": {
                        "symbol": symbol,
                        "positions": len(pairs),
                        "price": price,
                    }
                },
            )
        self.symbols_rescued += rescued
        return rescued

    async def _fetch_last_price(self, symbol: str) -> float | None:
        trades = await self._exchange.fetch_recent_trades(symbol, limit=1)
        if not trades:
            return None
        return float(trades[-1].price)
