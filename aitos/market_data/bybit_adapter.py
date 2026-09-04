"""Canonical Bybit public market-data adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .contracts import MarketEvent, MarketEventType, MarketSource
from .venues import MarketType, Venue, VenueCapabilities
from .websocket_adapter import JsonWebSocketAdapter


class BybitCanonicalMarketDataAdapter(JsonWebSocketAdapter):
    """Normalize Bybit public linear perpetual streams into MarketEvent."""

    websocket_url = "wss://stream.bybit.com/v5/public/linear"

    def __init__(self, market_type: MarketType = MarketType.USD_M_FUTURES) -> None:
        if market_type not in (MarketType.PERPETUAL, MarketType.USD_M_FUTURES):
            raise ValueError("Bybit public adapter currently supports linear derivatives only")
        self._market_type = market_type

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
        topics = [f"publicTrade.{s.upper()}" for s in symbols]
        async for event in self._stream(symbols, {"op": "subscribe", "args": topics}, self._parse_trade):
            yield event

    async def stream_order_books(self, symbols: list[str], levels: int = 50) -> AsyncIterator[MarketEvent]:
        depth = 50 if levels <= 50 else 200
        topics = [f"orderbook.{depth}.{s.upper()}" for s in symbols]
        async for event in self._stream(symbols, {"op": "subscribe", "args": topics}, self._parse_book):
            yield event

    def _parse_trade(self, message: dict[str, Any]) -> MarketEvent | list[MarketEvent] | None:
        topic = str(message.get("topic", ""))
        if not topic.startswith("publicTrade."):
            return None
        events: list[MarketEvent] = []
        for item in message.get("data") or []:
            symbol = str(item.get("s", "")).upper()
            if not symbol:
                continue
            trade_id = str(item.get("i") or item.get("seq") or "0")
            try:
                sequence = int(item.get("seq") or trade_id)
            except (TypeError, ValueError):
                sequence = None
            event_time = self._timestamp_ms(item.get("T") or message.get("ts"))
            events.append(MarketEvent(
                event_type=MarketEventType.TRADE,
                exchange=Venue.BYBIT.value,
                market=self.market_type.value,
                symbol=symbol,
                event_time=event_time,
                payload={"symbol": symbol, "timestamp": event_time.isoformat(), "trade_id": trade_id, "price": self._float(item["p"]), "quantity": self._float(item["v"]), "side": item.get("S")},
                source=MarketSource.WEBSOCKET,
                sequence=sequence,
                correlation_id=f"bybit:{self.market_type.value}:{symbol}:{trade_id}",
                trace_id=symbol,
            ))
        if not events:
            return None
        return events[0] if len(events) == 1 else events

    def _parse_book(self, message: dict[str, Any]) -> MarketEvent | None:
        topic = str(message.get("topic", ""))
        if not topic.startswith("orderbook."):
            return None
        data = message.get("data") or {}
        symbol = str(data.get("s", "")).upper()
        if not symbol:
            return None
        update_id = int(data.get("u") or data.get("seq") or 0)
        event_time = self._timestamp_ms(message.get("ts"))
        return MarketEvent(
            event_type=MarketEventType.BOOK_SNAPSHOT if message.get("type") == "snapshot" else MarketEventType.BOOK_DELTA,
            exchange=Venue.BYBIT.value,
            market=self.market_type.value,
            symbol=symbol,
            event_time=event_time,
            payload={
                "bids": [{"price": self._float(p), "quantity": self._float(q)} for p, q in data.get("b", []) if self._float(q) > 0],
                "asks": [{"price": self._float(p), "quantity": self._float(q)} for p, q in data.get("a", []) if self._float(q) > 0],
                "last_update_id": update_id,
                "update_id": update_id,
                "cross_sequence": data.get("seq"),
            },
            source=MarketSource.WEBSOCKET,
            sequence=update_id,
            correlation_id=f"bybit:{self.market_type.value}:{symbol}:book:{update_id}",
            trace_id=symbol,
        )
