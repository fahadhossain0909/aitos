"""Canonical Binance market-data adapter facade."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from aitos.exchange.base import ExchangeAdapter

from .contracts import MarketEvent, MarketSource
from .legacy_bridge import (
    book_delta_event,
    book_snapshot_event,
    kline_event,
    trade_event,
)
from .venues import MarketType, Venue, VenueCapabilities

KLINE_TIMEFRAME = "5m"
KLINE_SYMBOL_LIMIT = 5


class BinanceCanonicalMarketDataAdapter:
    """Normalize Binance exchange streams into the canonical event contract."""

    def __init__(
        self, exchange: ExchangeAdapter, market_type: str = "usd_m_futures"
    ) -> None:
        self.exchange = exchange
        self._market_type = MarketType(market_type)

    @property
    def venue(self) -> Venue:
        return Venue.BINANCE

    @property
    def market_type(self) -> MarketType:
        return self._market_type

    @property
    def capabilities(self) -> VenueCapabilities:
        return VenueCapabilities(
            trades=True,
            order_book=True,
            funding=True,
            open_interest=True,
            liquidations=True,
            options=True,
        )

    async def stream_trades(self, symbols: list[str]) -> AsyncIterator[MarketEvent]:
        """Stream trades plus a bounded 5-minute kline cohort.

        The caller supplies the scanner's live trade cohort (Top-50 after
        staging). The first five symbols are therefore the ranked Top-5 cohort
        and are the only symbols subscribed to the live kline feed. This keeps
        kline websocket pressure bounded while preserving live trade coverage.
        """
        trade_stream = self.exchange.stream_trades(symbols).__aiter__()
        kline_symbols = list(dict.fromkeys(s.upper() for s in symbols))[
            :KLINE_SYMBOL_LIMIT
        ]
        kline_stream = (
            self.exchange.stream_klines(kline_symbols, KLINE_TIMEFRAME).__aiter__()
            if kline_symbols
            else None
        )
        tasks: dict[asyncio.Task, str] = {
            asyncio.create_task(trade_stream.__anext__()): "trade"
        }
        if kline_stream is not None:
            tasks[asyncio.create_task(kline_stream.__anext__())] = "kline"

        try:
            while tasks:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    stream_kind = tasks.pop(task)
                    try:
                        item = task.result()
                    except StopAsyncIteration:
                        continue
                    if stream_kind == "trade":
                        tasks[asyncio.create_task(trade_stream.__anext__())] = "trade"
                        yield trade_event(
                            item,
                            market_type=self.market_type,
                            source=MarketSource.WEBSOCKET,
                        )
                    else:
                        tasks[asyncio.create_task(kline_stream.__anext__())] = "kline"  # type: ignore[union-attr]
                        yield kline_event(
                            item,
                            market_type=self.market_type,
                            source=MarketSource.WEBSOCKET,
                        )
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for stream in (trade_stream, kline_stream):
                if stream is not None and hasattr(stream, "aclose"):
                    try:
                        await stream.aclose()
                    except Exception:
                        pass

    async def stream_klines(
        self, symbols: list[str], timeframe: str = KLINE_TIMEFRAME
    ) -> AsyncIterator[MarketEvent]:
        """Stream normalized Binance klines for a bounded symbol set."""
        async for kline in self.exchange.stream_klines(
            list(dict.fromkeys(s.upper() for s in symbols))[:KLINE_SYMBOL_LIMIT],
            timeframe,
        ):
            yield kline_event(
                kline,
                market_type=self.market_type,
                source=MarketSource.WEBSOCKET,
            )

    async def stream_order_books(
        self, symbols: list[str], levels: int = 20
    ) -> AsyncIterator[MarketEvent]:
        async for book in self.exchange.stream_order_book(symbols, levels=levels):
            yield book_snapshot_event(
                book,
                market_type=self.market_type,
                source=MarketSource.WEBSOCKET,
            )

    async def stream_order_book_deltas(
        self, symbols: list[str]
    ) -> AsyncIterator[MarketEvent]:
        """Emit every Binance diff-depth update without sampling or aggregation."""
        stream = getattr(self.exchange, "stream_order_book_deltas", None)
        if stream is None:
            raise RuntimeError("Binance exchange adapter lacks raw depth-delta support")
        async for symbol, delta in stream(symbols):
            yield book_delta_event(
                delta,
                symbol=symbol,
                market_type=self.market_type,
                source=MarketSource.WEBSOCKET,
            )

    async def recover_trade_events(
        self, symbol: str, limit: int = 500
    ) -> list[MarketEvent]:
        trades = await self.exchange.fetch_recent_trades(symbol, limit=limit)
        return [
            trade_event(t, market_type=self.market_type, source=MarketSource.REST)
            for t in trades
        ]

    async def recover_order_book(self, symbol: str, levels: int = 50) -> MarketEvent:
        book = await self.exchange.fetch_order_book(symbol, limit=levels)
        return book_snapshot_event(
            book,
            market_type=self.market_type,
            source=MarketSource.REST,
        )

    @staticmethod
    def age_seconds(event: MarketEvent) -> float:
        return max(0.0, (datetime.now(timezone.utc) - event.event_time).total_seconds())
