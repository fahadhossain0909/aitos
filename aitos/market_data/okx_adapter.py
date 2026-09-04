"""Canonical OKX public market-data adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .contracts import MarketEvent, MarketEventType, MarketSource
from .venues import MarketType, Venue, VenueCapabilities
from .websocket_adapter import JsonWebSocketAdapter


class OKXCanonicalMarketDataAdapter(JsonWebSocketAdapter):
    """Normalize OKX public swap streams into venue-neutral events."""

    websocket_url = "wss://ws.okx.com:8443/ws/v5/public"

    def __init__(self, market_type: MarketType = MarketType.PERPETUAL) -> None:
        if market_type not in (MarketType.PERPETUAL, MarketType.USD_M_FUTURES):
            raise ValueError("OKX adapter currently supports perpetual derivatives")
        self._market_type = market_type
        self._books: dict[str, tuple[dict[float, float], dict[float, float], int]] = {}

    @property
    def venue(self) -> Venue:
        return Venue.OKX

    @property
    def market_type(self) -> MarketType:
        return self._market_type

    @property
    def capabilities(self) -> VenueCapabilities:
        return VenueCapabilities(trades=True, order_book=True, rest_recovery=True)

    def _args(self, symbols: list[str], channel: str) -> list[dict[str, str]]:
        return [
            {"channel": channel, "instId": self._instrument(s)}
            for s in dict.fromkeys(symbols)
        ]

    @staticmethod
    def _instrument(symbol: str) -> str:
        value = symbol.upper()
        if value.endswith("-USDT-SWAP"):
            return value
        if value.endswith("USDT"):
            return f"{value[:-4]}-USDT-SWAP"
        return value

    @staticmethod
    def _symbol(inst_id: str) -> str:
        value = inst_id.upper()
        if value.endswith("-USDT-SWAP"):
            return f"{value[:-10]}USDT"
        return value.replace("-", "")

    async def stream_trades(self, symbols: list[str]) -> AsyncIterator[MarketEvent]:
        message = {"op": "subscribe", "args": self._args(symbols, "trades")}
        async for event in self._stream(symbols, message, self._parse_trade):
            yield event

    async def stream_order_books(
        self, symbols: list[str], levels: int = 20
    ) -> AsyncIterator[MarketEvent]:
        channel = "books5" if levels <= 5 else "books"
        message = {"op": "subscribe", "args": self._args(symbols, channel)}
        async for event in self._stream(symbols, message, self._parse_book):
            yield event

    def _parse_trade(self, message: dict[str, Any]) -> MarketEvent | None:
        if message.get("arg", {}).get("channel") != "trades":
            return None
        data = message.get("data") or []
        if not data:
            return None
        item = data[0]
        symbol = self._symbol(str(item.get("instId", "")))
        if not symbol:
            return None
        trade_id = str(item.get("tradeId") or item.get("seqId") or "0")
        try:
            sequence = int(item.get("tradeId")) if item.get("tradeId") else None
        except (TypeError, ValueError):
            sequence = None
        event_time = self._timestamp_ms(item.get("ts"))
        return MarketEvent(
            event_type=MarketEventType.TRADE,
            exchange=Venue.OKX.value,
            market=self.market_type.value,
            market_type=self.market_type.value,
            symbol=symbol,
            instrument_id=f"okx:{self.market_type.value}:{self._instrument(symbol)}",
            event_time=event_time,
            payload={
                "symbol": symbol,
                "timestamp": event_time.isoformat(),
                "trade_id": trade_id,
                "price": self._float(item["px"]),
                "quantity": self._float(item["sz"]),
                "side": item.get("side"),
            },
            source=MarketSource.WEBSOCKET,
            sequence=sequence,
            correlation_id=f"okx:{self.market_type.value}:{symbol}:{trade_id}",
            trace_id=symbol,
        )

    def _parse_book(self, message: dict[str, Any]) -> MarketEvent | None:
        channel = message.get("arg", {}).get("channel")
        if channel not in {"books", "books5"}:
            return None
        data = message.get("data") or []
        if not data:
            return None
        item = data[0]
        symbol = self._symbol(str(item.get("instId", "")))
        if not symbol:
            return None
        action = item.get("action") or "snapshot"
        sequence = int(item.get("seqId") or 0)
        bids, asks, previous = self._books.get(symbol, ({}, {}, 0))
        if action == "snapshot" or symbol not in self._books:
            bids = {
                float(row[0]): float(row[1])
                for row in item.get("bids", [])
                if float(row[1]) > 0
            }
            asks = {
                float(row[0]): float(row[1])
                for row in item.get("asks", [])
                if float(row[1]) > 0
            }
        else:
            if sequence and previous and sequence <= previous:
                return None
            for row in item.get("bids", []):
                price, quantity = float(row[0]), float(row[1])
                if quantity == 0:
                    bids.pop(price, None)
                else:
                    bids[price] = quantity
            for row in item.get("asks", []):
                price, quantity = float(row[0]), float(row[1])
                if quantity == 0:
                    asks.pop(price, None)
                else:
                    asks[price] = quantity
        self._books[symbol] = (bids, asks, sequence or previous)
        event_time = self._timestamp_ms(item.get("ts"))
        return MarketEvent(
            event_type=MarketEventType.BOOK_SNAPSHOT,
            exchange=Venue.OKX.value,
            market=self.market_type.value,
            market_type=self.market_type.value,
            symbol=symbol,
            instrument_id=f"okx:{self.market_type.value}:{self._instrument(symbol)}",
            event_time=event_time,
            payload={
                "symbol": symbol,
                "timestamp": event_time.isoformat(),
                "bids": [
                    {"price": p, "quantity": q}
                    for p, q in sorted(bids.items(), reverse=True)
                ],
                "asks": [{"price": p, "quantity": q} for p, q in sorted(asks.items())],
                "last_update_id": sequence or previous,
            },
            source=MarketSource.WEBSOCKET,
            sequence=sequence or previous,
            correlation_id=f"okx:{self.market_type.value}:{symbol}:book:{sequence or previous}",
            trace_id=symbol,
        )
