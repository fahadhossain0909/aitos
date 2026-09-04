"""Canonical Bybit V5 public market-data adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .contracts import MarketEvent, MarketEventType, MarketSource
from .venues import MarketType, Venue, VenueCapabilities
from .websocket_adapter import JsonWebSocketAdapter


class BybitCanonicalMarketDataAdapter(JsonWebSocketAdapter):
    """Normalize Bybit public linear derivatives streams into MarketEvent.

    Bybit sends an initial order-book snapshot followed by deltas. The
    canonical runtime consumes snapshots, so this adapter maintains a small
    in-memory book and emits a normalized snapshot after every valid update.
    """

    websocket_url = "wss://stream.bybit.com/v5/public/linear"

    def __init__(self, market_type: MarketType = MarketType.USD_M_FUTURES) -> None:
        if market_type not in (MarketType.PERPETUAL, MarketType.USD_M_FUTURES):
            raise ValueError(
                "Bybit public adapter currently supports linear derivatives only"
            )
        self._market_type = market_type
        self._books: dict[str, tuple[dict[float, float], dict[float, float], int]] = {}

    @property
    def venue(self) -> Venue:
        return Venue.BYBIT

    @property
    def market_type(self) -> MarketType:
        return self._market_type

    @property
    def capabilities(self) -> VenueCapabilities:
        return VenueCapabilities(trades=True, order_book=True, rest_recovery=True)

    async def stream_trades(self, symbols: list[str]) -> AsyncIterator[MarketEvent]:
        topics = [f"publicTrade.{s.upper()}" for s in dict.fromkeys(symbols)]
        async for event in self._stream(
            symbols, {"op": "subscribe", "args": topics}, self._parse_trade
        ):
            yield event

    async def stream_order_books(
        self, symbols: list[str], levels: int = 50
    ) -> AsyncIterator[MarketEvent]:
        depth = 50 if levels <= 50 else 200
        topics = [f"orderbook.{depth}.{s.upper()}" for s in dict.fromkeys(symbols)]
        async for event in self._stream(
            symbols, {"op": "subscribe", "args": topics}, self._parse_book
        ):
            yield event

    def _parse_trade(self, message: dict[str, Any]) -> MarketEvent | None:
        topic = str(message.get("topic", ""))
        if not topic.startswith("publicTrade."):
            return None
        data = message.get("data") or []
        if not data:
            return None
        item = data[0]
        symbol = str(item.get("s", "")).upper()
        if not symbol:
            return None
        trade_id = str(item.get("i") or item.get("seq") or "0")
        try:
            sequence = int(item.get("seq")) if item.get("seq") is not None else None
        except (TypeError, ValueError):
            sequence = None
        event_time = self._timestamp_ms(item.get("T") or message.get("ts"))
        return MarketEvent(
            event_type=MarketEventType.TRADE,
            exchange=Venue.BYBIT.value,
            market=self.market_type.value,
            market_type=self.market_type.value,
            symbol=symbol,
            instrument_id=f"bybit:{self.market_type.value}:{symbol}",
            event_time=event_time,
            payload={
                "symbol": symbol,
                "timestamp": event_time.isoformat(),
                "trade_id": trade_id,
                "price": self._float(item["p"]),
                "quantity": self._float(item["v"]),
                "side": item.get("S"),
            },
            source=MarketSource.WEBSOCKET,
            sequence=sequence,
            correlation_id=f"bybit:{self.market_type.value}:{symbol}:{trade_id}",
            trace_id=symbol,
        )

    def _parse_book(self, message: dict[str, Any]) -> MarketEvent | None:
        topic = str(message.get("topic", ""))
        if not topic.startswith("orderbook."):
            return None
        data = message.get("data") or {}
        symbol = str(data.get("s", "")).upper()
        if not symbol:
            return None
        update_id = int(data.get("u") or 0)
        bids, asks, previous = self._books.get(symbol, ({}, {}, 0))
        if (
            message.get("type") == "snapshot"
            or symbol not in self._books
            or update_id <= 1
        ):
            bids = {float(p): float(q) for p, q in data.get("b", []) if float(q) > 0}
            asks = {float(p): float(q) for p, q in data.get("a", []) if float(q) > 0}
        else:
            if update_id <= previous:
                return None
            for price, quantity in data.get("b", []):
                p, q = float(price), float(quantity)
                if q == 0:
                    bids.pop(p, None)
                else:
                    bids[p] = q
            for price, quantity in data.get("a", []):
                p, q = float(price), float(quantity)
                if q == 0:
                    asks.pop(p, None)
                else:
                    asks[p] = q
        self._books[symbol] = (bids, asks, update_id)
        event_time = self._timestamp_ms(data.get("cts") or message.get("ts"))
        return MarketEvent(
            event_type=MarketEventType.BOOK_SNAPSHOT,
            exchange=Venue.BYBIT.value,
            market=self.market_type.value,
            market_type=self.market_type.value,
            symbol=symbol,
            instrument_id=f"bybit:{self.market_type.value}:{symbol}",
            event_time=event_time,
            payload={
                "symbol": symbol,
                "timestamp": event_time.isoformat(),
                "bids": [
                    {"price": p, "quantity": q}
                    for p, q in sorted(bids.items(), reverse=True)
                ],
                "asks": [{"price": p, "quantity": q} for p, q in sorted(asks.items())],
                "last_update_id": update_id,
            },
            source=MarketSource.WEBSOCKET,
            sequence=update_id,
            correlation_id=f"bybit:{self.market_type.value}:{symbol}:book:{update_id}",
            trace_id=symbol,
        )
