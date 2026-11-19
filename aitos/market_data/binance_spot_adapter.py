"""Canonical Binance Spot market-data adapter.

It emits the same MarketEvent envelope as Futures while retaining a distinct
binance:spot instrument identity. Depth subscriptions bootstrap from REST and
fail closed on a sequence gap; consumers must discard the book and resnapshot.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any

from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.market_data.venues import InstrumentKey, MarketType, Venue, VenueCapabilities
from aitos.spot.binance import BinanceSpotAdapter
from aitos.spot.websocket import BinanceSpotWebSocket, SpotOrderBookGap


def _event_time(value: Any) -> datetime:
    return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)


def _event(
    symbol: str,
    kind: MarketEventType,
    event_time: datetime,
    payload: dict[str, Any],
    source: MarketSource,
    sequence: int | None = None,
) -> MarketEvent:
    key = InstrumentKey(Venue.BINANCE, MarketType.SPOT, symbol.upper())
    return MarketEvent(
        event_type=kind,
        exchange=Venue.BINANCE.value,
        venue=Venue.BINANCE.value,
        market=MarketType.SPOT.value,
        market_type=MarketType.SPOT.value,
        symbol=symbol.upper(),
        instrument_id=key.value,
        event_time=event_time,
        payload=payload,
        source=source,
        sequence=sequence,
        correlation_id=f"{key.value}:{kind.value}:{sequence or event_time.isoformat()}",
    )


class BinanceSpotCanonicalMarketDataAdapter:
    """Normalize Binance Spot REST/WebSocket data to AITOS canonical events."""

    def __init__(
        self,
        rest: BinanceSpotAdapter | None = None,
        websocket: BinanceSpotWebSocket | None = None,
    ) -> None:
        self.rest = rest or BinanceSpotAdapter(testnet=True)
        self.websocket = websocket or BinanceSpotWebSocket(testnet=self.rest.testnet)

    @property
    def venue(self) -> Venue:
        return Venue.BINANCE

    @property
    def market_type(self) -> MarketType:
        return MarketType.SPOT

    @property
    def capabilities(self) -> VenueCapabilities:
        return VenueCapabilities(
            funding=False,
            open_interest=False,
            liquidations=False,
            options=False,
            trades=True,
            order_book=True,
            ticker=True,
            klines=True,
            instruments=True,
            rest_recovery=True,
        )

    async def instruments(self, quote_asset: str = "USDT"):
        return await self.rest.instruments(quote_asset)

    async def stream_trades(self, symbols: list[str]) -> AsyncIterator[MarketEvent]:
        async def source(symbol: str):
            async for item in self.websocket.stream(symbol, "@trade"):
                data = item["data"]
                yield _event(
                    symbol,
                    MarketEventType.TRADE,
                    _event_time(data["T"]),
                    {
                        "trade_id": int(data["t"]),
                        "price": str(data["p"]),
                        "quantity": str(data["q"]),
                        "buyer_is_maker": bool(data["m"]),
                        "trade_time_ms": int(data["T"]),
                    },
                    MarketSource.WEBSOCKET,
                    int(data["t"]),
                )

        async for event in _merge_symbols(_symbols(symbols), source):
            yield event

    async def stream_tickers(self, symbols: list[str]) -> AsyncIterator[MarketEvent]:
        """Stream best bid/ask and sizes from Binance bookTicker events."""

        async def source(symbol: str):
            async for item in self.websocket.stream(symbol, "@bookTicker"):
                data = item["data"]
                event_ms = data.get("E") or data.get("T")
                observed_at = (
                    _event_time(event_ms)
                    if event_ms is not None
                    else datetime.now(timezone.utc)
                )
                yield _event(
                    symbol,
                    MarketEventType.TICKER,
                    observed_at,
                    {
                        "bid_price": str(data["b"]),
                        "bid_quantity": str(data["B"]),
                        "ask_price": str(data["a"]),
                        "ask_quantity": str(data["A"]),
                    },
                    MarketSource.WEBSOCKET,
                    int(data.get("u", 0)) or None,
                )

        async for event in _merge_symbols(_symbols(symbols), source):
            yield event

    async def stream_klines(
        self, symbols: list[str], timeframe: str = "1m"
    ) -> AsyncIterator[MarketEvent]:
        async def source(symbol: str):
            async for item in self.websocket.stream(symbol, f"@kline_{timeframe}"):
                data = item["data"]["k"]
                if not bool(data["x"]):
                    continue
                yield _event(
                    symbol,
                    MarketEventType.KLINE,
                    _event_time(data["T"]),
                    {
                        "timeframe": timeframe,
                        "open_time_ms": int(data["t"]),
                        "close_time_ms": int(data["T"]),
                        "open": str(data["o"]),
                        "high": str(data["h"]),
                        "low": str(data["l"]),
                        "close": str(data["c"]),
                        "volume": str(data["v"]),
                        "quote_volume": str(data["q"]),
                        "trades": int(data["n"]),
                        "taker_buy_base": str(data["V"]),
                        "taker_buy_quote": str(data["Q"]),
                        "is_closed": True,
                    },
                    MarketSource.WEBSOCKET,
                    int(data["T"]),
                )

        async for event in _merge_symbols(_symbols(symbols), source):
            yield event

    async def stream_order_books(
        self, symbols: list[str], levels: int = 20
    ) -> AsyncIterator[MarketEvent]:
        async def source(symbol: str):
            snapshot = await self.rest.depth(symbol, limit=_depth_limit(levels))
            last_update_id = int(snapshot["lastUpdateId"])
            yield _event(
                symbol,
                MarketEventType.BOOK_SNAPSHOT,
                datetime.now(timezone.utc),
                {
                    "bids": [{"price": p, "quantity": q} for p, q in snapshot["bids"]],
                    "asks": [{"price": p, "quantity": q} for p, q in snapshot["asks"]],
                    "last_update_id": last_update_id,
                },
                MarketSource.REST,
                last_update_id,
            )
            previous_final = last_update_id
            bridged = False
            async for item in self.websocket.stream(symbol, "@depth@100ms"):
                data = item["data"]
                first, final = int(data["U"]), int(data["u"])
                if final <= previous_final:
                    continue
                if not bridged:
                    if not (first <= previous_final + 1 <= final):
                        if first > previous_final + 1:
                            raise SpotOrderBookGap(
                                f"{symbol} initial depth diff does not bridge snapshot"
                            )
                        continue
                    bridged = True
                else:
                    previous = data.get("pu")
                    if previous is not None and int(previous) != previous_final:
                        raise SpotOrderBookGap(f"{symbol} depth previous-update gap")
                    if previous is None and first > previous_final + 1:
                        raise SpotOrderBookGap(f"{symbol} depth sequence gap")
                previous_final = final
                yield _event(
                    symbol,
                    MarketEventType.BOOK_DELTA,
                    _event_time(data["E"]),
                    {
                        "first_update_id": first,
                        "final_update_id": final,
                        "previous_update_id": (
                            int(data["pu"]) if data.get("pu") is not None else None
                        ),
                        "bids": [{"price": p, "quantity": q} for p, q in data["b"]],
                        "asks": [{"price": p, "quantity": q} for p, q in data["a"]],
                    },
                    MarketSource.WEBSOCKET,
                    final,
                )

        async for event in _merge_symbols(_symbols(symbols), source):
            yield event

    async def close(self) -> None:
        await self.rest.close()


def _symbols(values: list[str]) -> list[str]:
    result = list(dict.fromkeys(str(value).upper() for value in values if value))
    if not result:
        raise ValueError("at least one Spot symbol is required")
    if len(result) > 100:
        raise ValueError("Spot adapter subscription is limited to 100 symbols per call")
    return result


def _depth_limit(levels: int) -> int:
    allowed = (5, 10, 20, 50, 100, 500, 1000, 5000)
    if levels not in allowed:
        raise ValueError(f"unsupported Spot depth limit: {levels}")
    return levels


async def _merge_symbols(
    symbols: list[str], factory: Callable[[str], AsyncIterator[MarketEvent]]
) -> AsyncIterator[MarketEvent]:
    """Run one bounded stream task per symbol and merge events fairly."""
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(
        maxsize=max(100, len(symbols) * 100)
    )

    async def worker(symbol: str) -> None:
        try:
            async for event in factory(symbol):
                await queue.put(("event", event))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(("error", exc))
        finally:
            task = asyncio.current_task()
            if task is None or not task.cancelling():
                await queue.put(("done", symbol))

    tasks = [
        asyncio.create_task(worker(symbol), name=f"spot-stream-{symbol}")
        for symbol in symbols
    ]
    completed = 0
    try:
        while completed < len(tasks):
            kind, value = await queue.get()
            if kind == "event":
                yield value
            elif kind == "error":
                raise value
            else:
                completed += 1
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
