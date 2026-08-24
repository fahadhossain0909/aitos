"""Binance USDT-M Futures exchange adapter."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import aiohttp

from aitos.exchange.base import ExchangeAdapter
from aitos.exchange.orderbook import LocalOrderBook, OrderBookSequenceError
from aitos.exchange.parsing import (
    parse_agg_trade_ws,
    parse_depth_diff_ws,
    parse_funding_rate_rest,
    parse_kline_rest,
    parse_kline_ws,
    parse_open_interest_rest,
    parse_order_book_rest,
    parse_trade_rest,
)
from aitos.exchange.rate_limiter import TokenBucketRateLimiter
from aitos.exchange.symbol_filters import SymbolFilters, parse_exchange_info
from aitos.logging_setup import get_logger
from aitos.models.market import (
    FundingRate,
    Kline,
    OpenInterest,
    OrderBookSnapshot,
    TradeTick,
)

logger = get_logger("aitos.exchange.binance")
REST_BASE_URL = "https://fapi.binance.com"
WS_BASE_URL = "wss://fstream.binance.com/stream"
WS_SINGLE_BASE_URL = "wss://fstream.binance.com/ws"
DEFAULT_RATE_LIMIT_CAPACITY = 2000
DEFAULT_RATE_LIMIT_REFILL_PER_SECOND = 2000 / 60
MAX_BACKOFF_SECONDS = 60.0
INITIAL_BACKOFF_SECONDS = 1.0
ORDERBOOK_BOOTSTRAP_QUEUE_SIZE = 5000
ORDERBOOK_BOOTSTRAP_READY_TIMEOUT_SECONDS = 10.0


class BinanceFuturesAdapter(ExchangeAdapter):
    def __init__(
        self,
        session_factory: Callable[[], aiohttp.ClientSession],
        ws_connector: Optional[Callable[..., Any]] = None,
        rate_limiter: Optional[TokenBucketRateLimiter] = None,
    ) -> None:
        self._session_factory = session_factory
        self._session: Optional[aiohttp.ClientSession] = None
        if ws_connector is None:
            import websockets

            ws_connector = websockets.connect
        self._ws_connector = ws_connector
        self._rate_limiter = rate_limiter or TokenBucketRateLimiter(
            capacity=DEFAULT_RATE_LIMIT_CAPACITY,
            refill_per_second=DEFAULT_RATE_LIMIT_REFILL_PER_SECOND,
        )

    async def connect(self) -> None:
        if self._session is None or self._session.closed:
            self._session = self._session_factory()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def fetch_klines(
        self, symbol: str, timeframe: str, limit: int = 500
    ) -> List[Kline]:
        weight = 5 if limit <= 100 else (10 if limit <= 500 else 25)
        raw = await self._get(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": timeframe, "limit": limit},
            weight,
        )
        return [
            parse_kline_rest(row, symbol=symbol, timeframe=timeframe) for row in raw
        ]

    async def fetch_order_book(
        self, symbol: str, limit: int = 50
    ) -> OrderBookSnapshot:
        weight = 2 if limit <= 50 else (5 if limit <= 100 else 10)
        raw = await self._get(
            "/fapi/v1/depth", {"symbol": symbol, "limit": limit}, weight
        )
        return parse_order_book_rest(raw, symbol=symbol)

    async def fetch_recent_trades(
        self, symbol: str, limit: int = 500
    ) -> List[TradeTick]:
        raw = await self._get(
            "/fapi/v1/trades", {"symbol": symbol, "limit": limit}, weight=5
        )
        return [parse_trade_rest(row, symbol=symbol) for row in raw]

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        return parse_funding_rate_rest(
            await self._get("/fapi/v1/premiumIndex", {"symbol": symbol}, weight=1)
        )

    async def fetch_open_interest(self, symbol: str) -> OpenInterest:
        return parse_open_interest_rest(
            await self._get("/fapi/v1/openInterest", {"symbol": symbol}, weight=1)
        )

    async def fetch_exchange_info(
        self, symbols: Optional[List[str]] = None
    ) -> Dict[str, SymbolFilters]:
        raw = await self._get("/fapi/v1/exchangeInfo", {}, weight=1)
        all_filters = parse_exchange_info(raw)
        return (
            all_filters
            if symbols is None
            else {s: all_filters[s] for s in symbols if s in all_filters}
        )

    async def stream_klines(
        self, symbols: List[str], timeframe: str
    ) -> AsyncIterator[Kline]:
        streams = [f"{s.lower()}@kline_{timeframe}" for s in symbols]

        async def _parse(data: Any) -> Kline:
            return parse_kline_ws(data)

        async for kline in self._stream(streams, _parse):
            yield kline

    async def stream_trades(self, symbols: List[str]) -> AsyncIterator[TradeTick]:
        """Consume Binance Futures aggTrade through one combined stream connection.

        A single combined socket avoids maintaining one long-lived connection per
        symbol. The envelope returned by Binance's /stream endpoint is unwrapped
        before parsing so callers receive normal TradeTick objects.
        """
        if not symbols:
            return

        streams = [f"{symbol.lower()}@aggTrade" for symbol in symbols]
        url = f"{WS_BASE_URL}?streams={'/'.join(streams)}"
        queue: asyncio.Queue[TradeTick] = asyncio.Queue(maxsize=5000)

        async def consume() -> None:
            backoff = INITIAL_BACKOFF_SECONDS
            while True:
                try:
                    async with self._ws_connector(url) as ws:
                        logger.info(
                            "connected to Binance combined trade stream",
                            extra={
                                "aitos_extra": {
                                    "streams": streams,
                                    "mode": "combined-stream",
                                }
                            },
                        )
                        backoff = INITIAL_BACKOFF_SECONDS
                        async for raw_message in ws:
                            try:
                                envelope = json.loads(raw_message)
                                payload = envelope.get("data", envelope)
                                trade = parse_agg_trade_ws(payload)
                            except Exception as exc:
                                logger.error(
                                    "invalid Binance aggTrade message",
                                    extra={
                                        "aitos_extra": {
                                            "streams": streams,
                                            "error": str(exc),
                                        }
                                    },
                                )
                                continue
                            await queue.put(trade)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "Binance combined trade stream disconnected, reconnecting",
                        extra={
                            "aitos_extra": {
                                "streams": streams,
                                "error": str(exc),
                                "backoff_seconds": backoff,
                            }
                        },
                    )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        task = asyncio.create_task(consume(), name="binance-trade-combined")
        try:
            while True:
                yield await queue.get()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def stream_order_book(
        self, symbols: List[str], levels: int = 20
    ) -> AsyncIterator[OrderBookSnapshot]:
        """Reconstruct a local L2 book with loss-aware Binance bootstrap."""
        if not symbols:
            return

        streams = [f"{s.lower()}@depth@100ms" for s in symbols]
        symbol_by_stream = {f"{s.lower()}@depth@100ms": s for s in symbols}
        queue: asyncio.Queue[tuple[Any, str]] = asyncio.Queue(
            maxsize=ORDERBOOK_BOOTSTRAP_QUEUE_SIZE
        )
        producer_done = asyncio.Event()
        producer_ready = asyncio.Event()

        async def producer() -> None:
            try:
                async for data, stream_name in self._raw_stream(
                    streams, emit_reconnect=True
                ):
                    producer_ready.set()
                    try:
                        queue.put_nowait((data, stream_name))
                    except asyncio.QueueFull:
                        logger.error(
                            "order-book bootstrap buffer overflow; forcing resync",
                            extra={"aitos_extra": {"streams": streams}},
                        )
                        while not queue.empty():
                            try:
                                queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                        queue.put_nowait((data, stream_name))
            except asyncio.CancelledError:
                raise
            finally:
                producer_done.set()

        producer_task = asyncio.create_task(
            producer(), name="binance-orderbook-producer"
        )
        books: Dict[str, LocalOrderBook] = {}
        try:
            try:
                await asyncio.wait_for(
                    producer_ready.wait(),
                    timeout=ORDERBOOK_BOOTSTRAP_READY_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                raise RuntimeError("Binance order-book stream did not become ready")
            while True:
                data, stream_name = await queue.get()
                symbol = symbol_by_stream.get(stream_name)
                if symbol is None:
                    continue
                if symbol not in books:
                    books[symbol] = LocalOrderBook(symbol=symbol, levels=levels)
                try:
                    event = parse_depth_diff_ws(data, symbol=symbol)
                    snapshot = await books[symbol].apply(
                        event, self.fetch_order_book
                    )
                except OrderBookSequenceError:
                    books[symbol] = LocalOrderBook(symbol=symbol, levels=levels)
                    snapshot = None
                if snapshot is not None:
                    yield snapshot
        finally:
            producer_task.cancel()
            await asyncio.gather(producer_task, return_exceptions=True)

    async def _get(self, path: str, params: Dict[str, Any], weight: int) -> Any:
        await self._rate_limiter.acquire(weight)
        await self.connect()
        assert self._session is not None
        async with self._session.get(
            f"{REST_BASE_URL}{path}", params=params
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def _stream(
        self, streams: List[str], parser: Callable[[Any], Any]
    ) -> AsyncIterator[Any]:
        async for data, _stream_name in self._raw_stream(streams):
            yield await parser(data)

    async def _raw_stream(
        self, streams: List[str], emit_reconnect: bool = False
    ) -> AsyncIterator[tuple[Any, str]]:
        if not streams:
            return
        url = f"{WS_BASE_URL}?streams={'/'.join(streams)}"
        backoff = INITIAL_BACKOFF_SECONDS
        while True:
            try:
                async with self._ws_connector(url) as ws:
                    backoff = INITIAL_BACKOFF_SECONDS
                    async for raw_message in ws:
                        envelope = json.loads(raw_message)
                        stream_name = envelope.get("stream", "")
                        yield envelope.get("data", envelope), stream_name
                if emit_reconnect:
                    logger.warning(
                        "Binance combined stream closed, reconnecting",
                        extra={
                            "aitos_extra": {
                                "streams": streams,
                                "backoff_seconds": backoff,
                            }
                        },
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Binance combined stream disconnected, reconnecting",
                    extra={
                        "aitos_extra": {
                            "streams": streams,
                            "error": str(exc),
                            "backoff_seconds": backoff,
                        }
                    },
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
