"""Canonical OKX public market-data adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .contracts import MarketEvent, MarketEventType, MarketSource
from .venues import MarketType, Venue, VenueCapabilities
from .websocket_adapter import JsonWebSocketAdapter


class OKXCanonicalMarketDataAdapter(JsonWebSocketAdapter):
    """Normalize OKX public perpetual streams into MarketEvent."""

    websocket_url = "wss://ws.okx.com:8443/ws/v5/public"

    def __init__(self, market_type: MarketType = MarketType.PERPETUAL) -> None:
        if market_type not in (MarketType.PERPETUAL, MarketType.USD_M_FUTURES):
            raise ValueError(
                "OKX adapter currently supports swap/linear derivatives only"
            )
        self._market_type = market_type

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
        return [{"channel": channel, "instId": self._instrument(s)} for s in symbols]

    @staticmethod
    def _instrument(symbol: str) -> str:
        value = symbol.upper()
        if "-" in value:
            return value
        if value.endswith("USDT"):
            base = value[:-4]
            return f"{base}-USDT-SWAP"
        return value

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
        symbol = str(item.get("instId", "")).upper()
        if not symbol:
            return None
        trade_id = str(item.get("tradeId") or item.get("seqId") or "0")
        try:
            sequence = int(trade_id)
        except ValueError:
            sequence = None
        event_time = self._timestamp_ms(item.get("ts"))
        return MarketEvent(
            event_type=MarketEventType.TRADE,
            exchange=Venue.OKX.value,
            market=self.market_type.value,
            symbol=symbol,
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
        symbol = str(item.get("instId", "")).upper()
        if not symbol:
            return None
        sequence = int(item.get("seqId") or item.get("checksum") or 0)
        return MarketEvent(
            event_type=(
                MarketEventType.BOOK_SNAPSHOT
                if item.get("action") in (None, "snapshot")
                else MarketEventType.BOOK_DELTA
            ),
            exchange=Venue.OKX.value,
            market=self.market_type.value,
            symbol=symbol,
            event_time=self._timestamp_ms(item.get("ts")),
            payload={
                "bids": [
                    {"price": self._float(row[0]), "quantity": self._float(row[1])}
                    for row in item.get("bids", [])
                ],
                "asks": [
                    {"price": self._float(row[0]), "quantity": self._float(row[1])}
                    for row in item.get("asks", [])
                ],
                "last_update_id": sequence,
                "seq_id": sequence,
                "checksum": item.get("checksum"),
            },
            source=MarketSource.WEBSOCKET,
            sequence=sequence,
            correlation_id=f"okx:{self.market_type.value}:{symbol}:book:{sequence}",
            trace_id=symbol,
        )
