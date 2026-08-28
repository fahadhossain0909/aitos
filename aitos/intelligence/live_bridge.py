"""In-process fast lane for exchange market data.

The bridge is deliberately process-local: live trading consumers receive data
without putting Redis on the latency-critical path. Redis remains the durable
event bus for persistence, replay, and non-latency-critical consumers.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable

from aitos.logging_setup import get_logger
from aitos.models.market import OrderBookSnapshot, TradeTick

logger = get_logger("aitos.intelligence.live_bridge")

TradeHandler = Callable[[TradeTick], Awaitable[None]]
BookHandler = Callable[[OrderBookSnapshot], Awaitable[None]]

_trade_handlers: dict[str, list[TradeHandler]] = defaultdict(list)
_book_handlers: dict[str, list[BookHandler]] = defaultdict(list)


def register_trade_handler(symbol: str, handler: TradeHandler) -> None:
    if handler not in _trade_handlers[symbol]:
        _trade_handlers[symbol].append(handler)


def register_book_handler(symbol: str, handler: BookHandler) -> None:
    if handler not in _book_handlers[symbol]:
        _book_handlers[symbol].append(handler)


def unregister_trade_handler(symbol: str, handler: TradeHandler) -> None:
    handlers = _trade_handlers.get(symbol, [])
    if handler in handlers:
        handlers.remove(handler)


def unregister_book_handler(symbol: str, handler: BookHandler) -> None:
    handlers = _book_handlers.get(symbol, [])
    if handler in handlers:
        handlers.remove(handler)


async def publish_trade(trade: TradeTick) -> int:
    handlers = tuple(_trade_handlers.get(trade.symbol, ()))
    if not handlers:
        return 0
    results = await asyncio.gather(
        *(handler(trade) for handler in handlers), return_exceptions=True
    )
    for result in results:
        if isinstance(result, Exception):
            logger.exception(
                "direct live trade handler failed",
                extra={
                    "aitos_extra": {
                        "symbol": trade.symbol,
                        "trade_id": trade.trade_id,
                        "error_type": type(result).__name__,
                        "error": str(result),
                    }
                },
            )
    return len(handlers)


async def publish_book(book: OrderBookSnapshot) -> int:
    handlers = tuple(_book_handlers.get(book.symbol, ()))
    if not handlers:
        return 0
    results = await asyncio.gather(
        *(handler(book) for handler in handlers), return_exceptions=True
    )
    for result in results:
        if isinstance(result, Exception):
            logger.exception(
                "direct live order-book handler failed",
                extra={
                    "aitos_extra": {
                        "symbol": book.symbol,
                        "last_update_id": book.last_update_id,
                        "error_type": type(result).__name__,
                        "error": str(result),
                    }
                },
            )
    return len(handlers)
