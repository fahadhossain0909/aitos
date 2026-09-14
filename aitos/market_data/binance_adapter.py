"""Canonical Binance market-data adapter facade."""

from __future__ import annotations

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

KLINE_TIMEFRAME = "1m"
KLINE_SYMBOL_LIMIT = 5


class BinanceCanonicalMarketDataAdapter:
    """Normalize Binance exchange streams into the canonical event contract."""

    def __init__(
        self, exchange: ExchangeAdapter, market_type: str = "usd_m_futures"
    ) -> None:
        self.exchange = exchange
        self._market_type = MarketType(market_type)
        self._valid_symbol_cache: set[str] | None = None

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

    async def _valid_symbols(self, symbols: list[str] | tuple[str, ...]) -> list[str]:
        """Filter subscriptions against Binance exchange-info before opening sockets."""
        requested = list(dict.fromkeys(s.upper() for s in symbols if s))
        if not requested:
            return []
        missing = [
            s
            for s in requested
            if self._valid_symbol_cache is None or s not in self._valid_symbol_cache
        ]
        if self._valid_symbol_cache is None or missing:
            try:
                filters = await self.exchange.fetch_exchange_info(None)
            except Exception:
                return []
            self._valid_symbol_cache = set(filters)
        valid = [s for s in requested if s in (self._valid_symbol_cache or set())]
        rejected = [
            s for s in requested if s not in (self._valid_symbol_cache or set())
        ]
        if rejected:
            from aitos.logging_setup import get_logger

            get_logger("aitos.market_data.binance_adapter").warning(
                "rejected invalid Binance market-data symbols",
                extra={"aitos_extra": {"rejected_symbols": rejected}},
            )
        return valid

    async def stream_trades(self, symbols: list[str]) -> AsyncIterator[MarketEvent]:
        """Stream only the requested, exchange-valid trade cohort."""
        bounded = await self._valid_symbols(symbols)
        async for trade in self.exchange.stream_trades(bounded):
            yield trade_event(
                trade,
                market_type=self.market_type,
                source=MarketSource.WEBSOCKET,
            )

    async def stream_klines(
        self, symbols: list[str], timeframe: str = KLINE_TIMEFRAME
    ) -> AsyncIterator[MarketEvent]:
        """Stream normalized 1-minute Binance klines for at most five symbols."""
        bounded = (await self._valid_symbols(symbols))[:KLINE_SYMBOL_LIMIT]
        async for kline in self.exchange.stream_klines(bounded, timeframe):
            yield kline_event(
                kline,
                market_type=self.market_type,
                source=MarketSource.WEBSOCKET,
            )

    async def stream_order_books(
        self, symbols: list[str], levels: int = 20
    ) -> AsyncIterator[MarketEvent]:
        """Publish a REST seed immediately, then continue with live depth updates."""
        bounded = await self._valid_symbols(symbols)
        for symbol in bounded:
            try:
                yield await self.recover_order_book(symbol, levels=levels)
            except Exception:
                continue
        async for book in self.exchange.stream_order_book(bounded, levels=levels):
            yield book_snapshot_event(
                book,
                market_type=self.market_type,
                source=MarketSource.WEBSOCKET,
            )

    async def stream_trades_managed(
        self, symbols: list[str]
    ) -> "_ManagedCanonicalTradeStream":
        """Long-lived trade subscription; ``update_symbols`` reconfigures it
        in place via incremental SUBSCRIBE/UNSUBSCRIBE instead of a full
        websocket reconnect.
        """
        bounded = await self._valid_symbols(symbols)
        raw = self.exchange.stream_trades_managed(bounded)
        return _ManagedCanonicalTradeStream(self, raw)

    async def stream_klines_managed(
        self, symbols: list[str], timeframe: str = KLINE_TIMEFRAME
    ) -> "_ManagedCanonicalKlineStream":
        """Long-lived kline subscription; ``update_symbols`` reconfigures it
        in place instead of reconnecting.
        """
        bounded = (await self._valid_symbols(symbols))[:KLINE_SYMBOL_LIMIT]
        raw = self.exchange.stream_klines_managed(bounded, timeframe)
        return _ManagedCanonicalKlineStream(self, raw)

    async def stream_order_books_managed(
        self, symbols: list[str], levels: int = 20
    ) -> "_ManagedCanonicalOrderBookStream":
        """Long-lived orderbook subscription; ``update_symbols`` adds/drops
        symbols in place, preserving already-bootstrapped local order books
        for symbols that stay subscribed instead of re-bootstrapping every
        symbol on every change.
        """
        bounded = await self._valid_symbols(symbols)
        raw = self.exchange.stream_order_book_managed(bounded, levels=levels)
        return _ManagedCanonicalOrderBookStream(self, raw)

    async def stream_order_book_deltas(
        self, symbols: list[str]
    ) -> AsyncIterator[MarketEvent]:
        """Emit every Binance diff-depth update without sampling or aggregation."""
        bounded = await self._valid_symbols(symbols)
        stream = getattr(self.exchange, "stream_order_book_deltas", None)
        if stream is None:
            raise RuntimeError("Binance exchange adapter lacks raw depth-delta support")
        async for symbol, delta in stream(bounded):
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


class _ManagedCanonicalTradeStream:
    """MarketEvent view over a raw managed trade stream.

    ``update_symbols`` validates against exchange info (like the one-shot
    ``stream_trades`` path) and then reconfigures the *existing* websocket
    connection in place -- it never cancels or reconnects anything.
    """

    def __init__(self, parent: BinanceCanonicalMarketDataAdapter, raw) -> None:
        self._parent = parent
        self._raw = raw

    def __aiter__(self) -> "_ManagedCanonicalTradeStream":
        return self

    async def __anext__(self) -> MarketEvent:
        trade = await self._raw.__anext__()
        return trade_event(
            trade, market_type=self._parent.market_type, source=MarketSource.WEBSOCKET
        )

    async def update_symbols(self, symbols: list[str]) -> bool:
        bounded = await self._parent._valid_symbols(symbols)
        return await self._raw.update_symbols(bounded)

    async def aclose(self) -> None:
        await self._raw.aclose()


class _ManagedCanonicalKlineStream:
    """MarketEvent view over a raw managed kline stream (see
    ``_ManagedCanonicalTradeStream``)."""

    def __init__(self, parent: BinanceCanonicalMarketDataAdapter, raw) -> None:
        self._parent = parent
        self._raw = raw

    def __aiter__(self) -> "_ManagedCanonicalKlineStream":
        return self

    async def __anext__(self) -> MarketEvent:
        kline = await self._raw.__anext__()
        return kline_event(
            kline, market_type=self._parent.market_type, source=MarketSource.WEBSOCKET
        )

    async def update_symbols(self, symbols: list[str]) -> bool:
        bounded = (await self._parent._valid_symbols(symbols))[:KLINE_SYMBOL_LIMIT]
        return await self._raw.update_symbols(bounded)

    async def aclose(self) -> None:
        await self._raw.aclose()


class _ManagedCanonicalOrderBookStream:
    """MarketEvent view over a raw managed orderbook stream (see
    ``_ManagedCanonicalTradeStream``). Symbols that stay subscribed across an
    ``update_symbols`` call keep their already-bootstrapped local book."""

    def __init__(self, parent: BinanceCanonicalMarketDataAdapter, raw) -> None:
        self._parent = parent
        self._raw = raw

    def __aiter__(self) -> "_ManagedCanonicalOrderBookStream":
        return self

    async def __anext__(self) -> MarketEvent:
        book = await self._raw.__anext__()
        return book_snapshot_event(
            book, market_type=self._parent.market_type, source=MarketSource.WEBSOCKET
        )

    async def update_symbols(self, symbols: list[str]) -> bool:
        bounded = await self._parent._valid_symbols(symbols)
        return await self._raw.update_symbols(bounded)

    async def update_levels(self, levels: int) -> None:
        update = getattr(self._raw, "update_levels", None)
        if update is not None:
            await update(levels)

    async def aclose(self) -> None:
        await self._raw.aclose()
