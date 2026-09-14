"""REST-polling safety net for open-position SL/TP monitoring.

Root-cause context: ``TradeLifecycle.update_price()`` -- which contains the
authoritative, never-skipped hard-SL check -- only ever runs when a
``market.trade``/``market.kline`` event arrives via the live websocket
pipeline for that symbol. If that pipeline stalls for any reason (a
reconnect, a freshness gap, a symbol briefly falling out of the subscribed
set), any open position in that symbol silently stops being checked at
all -- with no fallback, since only the scanner has a REST fallback path
(``aitos.intelligence.scanner``'s ``rest_fallback``), not position
monitoring. A stop-loss can be breached for hours with the system never
noticing, purely because no price update ever arrived to trigger the check
-- not because the check itself is wrong.

This polls a lightweight REST endpoint for any open-position symbol whose
last live price update is older than ``stale_after_seconds``, and feeds the
result through the exact same ``update_price()`` call the live path uses,
so hard-SL/TP enforcement is never fully dependent on an uninterrupted
websocket connection. It is a safety net, not a replacement: the live path
remains the primary, low-latency source, and the poll interval is
deliberately coarse (default every 10s, only for symbols stale >20s).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any

from aitos.logging_setup import get_logger
from aitos.trading.price_freshness import PriceFreshnessTracker, get_global_tracker

logger = get_logger("aitos.trading.price_safety_net")

DEFAULT_STALE_AFTER_SECONDS = 20.0
DEFAULT_POLL_INTERVAL_SECONDS = 10.0


class PositionPriceSafetyNet:
    def __init__(
        self,
        exchange: Any,
        lifecycles_provider: Callable[[], Iterable[Any]],
        *,
        freshness: PriceFreshnessTracker | None = None,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._exchange = exchange
        self._lifecycles_provider = lifecycles_provider
        self._freshness = freshness or get_global_tracker()
        self._stale_after = stale_after_seconds
        self._interval = poll_interval_seconds
        self._task: asyncio.Task | None = None
        self._stopped = True
        self.polls_run = 0
        self.symbols_rescued = 0

    def start(self) -> None:
        if not self._stopped:
            return
        self._stopped = False
        self._task = asyncio.create_task(
            self._run(), name="position-price-safety-net"
        )

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

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
