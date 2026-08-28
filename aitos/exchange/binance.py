"""Binance USDT-M Futures exchange adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

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
    parse_trade_ws,
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
WS_MARKET_BASE_URL = "wss://fstream.binance.com/market/stream"
WS_PUBLIC_BASE_URL = "wss://fstream.binance.com/public/stream"
DEFAULT_RATE_LIMIT_CAPACITY = 2000
DEFAULT_RATE_LIMIT_REFILL_PER_SECOND = 2000 / 60
MAX_BACKOFF_SECONDS = 60.0
INITIAL_BACKOFF_SECONDS = 1.0
ORDERBOOK_BOOTSTRAP_QUEUE_SIZE = 5000
ORDERBOOK_BOOTSTRAP_READY_TIMEOUT_SECONDS = 10.0
WS_PING_INTERVAL_SECONDS = 15.0
WS_PING_TIMEOUT_SECONDS = 10.0
WS_OPEN_TIMEOUT_SECONDS = 10.0
TRADE_STREAM_IDLE_FALLBACK_SECONDS = 5.0
TRADE_STREAM_PRIMARY_RETRY_SECONDS = 1.0


class BinanceFuturesAdapter(ExchangeAdapter):
    def __init__(
        self,
        session_factory: Callable[[], aiohttp.ClientSession] = aiohttp.ClientSession,
        ws_connector: Callable[..., Any] | None = None,
        rate_limiter: TokenBucketRateLimiter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._session: aiohttp.ClientSession | None = None
        if ws_connector is None:
            import websockets

            def _default_connector(url: str):
                return websockets.connect(
                    url,
                    ping_interval=WS_PING_INTERVAL_SECONDS,
                    ping_timeout=WS_PING_TIMEOUT_SECONDS,
                    open_timeout=WS_OPEN_TIMEOUT_SECONDS,
                )

            ws_connector = _default_connector
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

    async def fetch_klines(self, symbol: str, timeframe: str, limit: int = 500) -> list[Kline]:
        weight = 5 if limit <= 100 else (10 if limit <= 500 else 25)
        raw = await self._get("/fapi/v1/klines", {"symbol": symbol, "interval": timeframe, "limit": limit}, weight)
        return [parse_kline_rest(row, symbol=symbol, timeframe=timeframe) for row in raw]

    async def fetch_order_book(self, symbol: str, limit: int = 50) -> OrderBookSnapshot:
        weight = 2 if limit <= 50 else (5 if limit <= 100 else 10)
        raw = await self._get("/fapi/v1/depth", {"symbol": symbol, "limit": limit}, weight)
        return parse_order_book_rest(raw, symbol=symbol)

    async def fetch_recent_trades(self, symbol: str, limit: int = 500) -> list[TradeTick]:
        raw = await self._get("/fapi/v1/trades", {"symbol": symbol, "limit": limit}, weight=5)
        return [parse_trade_rest(row, symbol=symbol) for row in raw]

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        return parse_funding_rate_rest(await self._get("/fapi/v1/premiumIndex", {"symbol": symbol}, weight=1))

    async def fetch_open_interest(self, symbol: str) -> OpenInterest:
        return parse_open_interest_rest(await self._get("/fapi/v1/openInterest", {"symbol": symbol}, weight=1))

    async def fetch_exchange_info(self, symbols: list[str] | None = None) -> dict[str, SymbolFilters]:
        raw = await self._get("/fapi/v1/exchangeInfo", {}, weight=1)
        all_filters = parse_exchange_info(raw)
        return all_filters if symbols is None else {s: all_filters[s] for s in symbols if s in all_filters}

    async def stream_klines(self, symbols: list[str], timeframe: str) -> AsyncIterator[Kline]:
        streams = [f"{s.lower()}@kline_{timeframe}" for s in symbols]

        async def _parse(data: Any) -> Kline:
            return parse_kline_ws(data)

        async for kline in self._stream(streams, _parse):
            yield kline

    async def stream_trades(self, symbols: list[str]) -> AsyncIterator[TradeTick]:
        """Keep every symbol live with per-symbol primary/fallback recovery.

        Each symbol prefers Binance ``@aggTrade``. If that symbol's primary
        stream is silent for five seconds, its ``@trade`` stream is used as a
        temporary live fallback. The primary stream is monitored concurrently
        and immediately resumes when it produces a valid event. Other symbols
        are unaffected by one symbol's fallback state.
        """
        if not symbols:
            return

        async def consume_symbol(symbol: str) -> AsyncIterator[TradeTick]:
            aggregate_stream = [f"{symbol.lower()}@aggTrade"]
            raw_trade_stream = [f"{symbol.lower()}@trade"]
            primary = self._raw_stream(aggregate_stream, emit_reconnect=True).__aiter__()
            fallback = self._raw_stream(raw_trade_stream, emit_reconnect=True).__aiter__()
            primary_task: asyncio.Task | None = None
            fallback_task: asyncio.Task | None = None
            primary_last_data = asyncio.get_running_loop().time()
            using_fallback = False

            try:
                while True:
                    if primary_task is None:
                        primary_task = asyncio.create_task(primary.__anext__())
                    if using_fallback and fallback_task is None:
                        fallback_task = asyncio.create_task(fallback.__anext__())

                    if not using_fallback:
                        done, _ = await asyncio.wait({primary_task}, return_when=asyncio.FIRST_COMPLETED)
                        if primary_task in done:
                            try:
                                data, _ = primary_task.result()
                                primary_task = None
                                primary_last_data = asyncio.get_running_loop().time()
                                yield parse_agg_trade_ws(data)
                            except StopAsyncIteration:
                                primary_task = None
                            continue

                    now = asyncio.get_running_loop().time()
                    if not using_fallback and now - primary_last_data >= TRADE_STREAM_IDLE_FALLBACK_SECONDS:
                        using_fallback = True
                        logger.warning(
                            "Binance aggregate-trade stream idle; entering per-symbol raw-trade fallback",
                            extra={"aitos_extra": {"symbol": symbol, "idle_seconds": TRADE_STREAM_IDLE_FALLBACK_SECONDS}},
                        )
                        fallback_task = asyncio.create_task(fallback.__anext__())

                    if using_fallback:
                        tasks = {task for task in (primary_task, fallback_task) if task is not None}
                        if not tasks:
                            continue
                        done, _ = await asyncio.wait(tasks, timeout=TRADE_STREAM_PRIMARY_RETRY_SECONDS, return_when=asyncio.FIRST_COMPLETED)
                        if not done:
                            continue
                        if primary_task is not None and primary_task in done:
                            try:
                                data, _ = primary_task.result()
                                primary_task = None
                                logger.info(
                                    "Binance aggregate-trade stream recovered; returning from per-symbol fallback",
                                    extra={"aitos_extra": {"symbol": symbol}},
                                )
                                using_fallback = False
                                yield parse_agg_trade_ws(data)
                                continue
                            except StopAsyncIteration:
                                primary_task = None
                        if fallback_task is not None and fallback_task in done:
                            try:
                                data, _ = fallback_task.result()
                                fallback_task = None
                                yield parse_trade_ws(data)
                            except StopAsyncIteration:
                                fallback_task = None
            except asyncio.CancelledError:
                raise
            finally:
                for task in (primary_task, fallback_task):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*(task for task in (primary_task, fallback_task) if task is not None), return_exceptions=True)
                await primary.aclose()
                await fallback.aclose()

        queues: dict[str, asyncio.Queue[TradeTick | BaseException | None]] = {
            symbol: asyncio.Queue(maxsize=1000) for symbol in symbols
        }

        async def worker(symbol: str) -> None:
            try:
                async for tick in consume_symbol(symbol):
                    await queues[symbol].put(tick)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                await queues[symbol].put(exc)
            finally:
                await queues[symbol].put(None)

        workers = [asyncio.create_task(worker(symbol), name=f"binance-trades-{symbol}") for symbol in symbols]
        try:
            active = set(symbols)
            while active:
                gets = {symbol: asyncio.create_task(queues[symbol].get()) for symbol in active}
                done, pending = await asyncio.wait(gets.values(), return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    symbol = next(key for key, value in gets.items() if value is task)
                    item = task.result()
                    if item is None:
                        active.discard(symbol)
                    elif isinstance(item, BaseException):
                        raise item
                    else:
                        yield item
        finally:
            for worker_task in workers:
                worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def stream_order_book(self, symbols: list[str], levels: int = 20) -> AsyncIterator[OrderBookSnapshot]:
        if not symbols:
            return
        streams = [f"{s.lower()}@depth@100ms" for s in symbols]
        symbol_by_stream = {f"{s.lower()}@depth@100ms": s for s in symbols}
        queue: asyncio.Queue[tuple[Any, str]] = asyncio.Queue(maxsize=ORDERBOOK_BOOTSTRAP_QUEUE_SIZE)
        producer_ready = asyncio.Event()

        async def producer() -> None:
            try:
                async for data, stream_name in self._raw_stream(streams, emit_reconnect=True):
                    producer_ready.set()
                    try:
                        queue.put_nowait((data, stream_name))
                    except asyncio.QueueFull:
                        logger.error("order-book bootstrap buffer overflow; forcing resync", extra={"aitos_extra": {"streams": streams}})
                        while not queue.empty():
                            try:
                                queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                        queue.put_nowait((data, stream_name))
            except asyncio.CancelledError:
                raise

        async def bootstrap(symbol: str) -> LocalOrderBook:
            book = LocalOrderBook(symbol=symbol, max_levels=levels)
            snapshot = await self.fetch_order_book(symbol, limit=max(levels, 50))
            book.seed(snapshot)
            return book

        producer_task = asyncio.create_task(producer(), name="binance-orderbook-producer")
        books: dict[str, LocalOrderBook] = {}
        try:
            try:
                await asyncio.wait_for(producer_ready.wait(), timeout=ORDERBOOK_BOOTSTRAP_READY_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                raise RuntimeError("Binance order-book stream did not become ready")
            for symbol in symbols:
                books[symbol] = await bootstrap(symbol)
            while True:
                data, stream_name = await queue.get()
                symbol = symbol_by_stream.get(stream_name)
                if symbol is None:
                    continue
                if symbol not in books:
                    books[symbol] = await bootstrap(symbol)
                try:
                    event = parse_depth_diff_ws(data)
                    snapshot = books[symbol].apply(event)
                except OrderBookSequenceError:
                    logger.warning("order-book sequence break; reseeding from REST", extra={"aitos_extra": {"symbol": symbol}})
                    books[symbol] = await bootstrap(symbol)
                    continue
                if snapshot is not None:
                    yield snapshot
        finally:
            producer_task.cancel()
            await asyncio.gather(producer_task, return_exceptions=True)

    async def _get(self, path: str, params: dict[str, Any], weight: int) -> Any:
        await self._rate_limiter.acquire(weight)
        await self.connect()
        assert self._session is not None
        async with self._session.get(f"{REST_BASE_URL}{path}", params=params) as response:
            response.raise_for_status()
            return await response.json()

    async def _stream(self, streams: list[str], parser: Callable[[Any], Any]) -> AsyncIterator[Any]:
        async for data, _stream_name in self._raw_stream(streams):
            yield await parser(data)

    @staticmethod
    def _ws_base_url(streams: list[str]) -> str:
        if not streams:
            return WS_MARKET_BASE_URL
        if all("@depth" in stream or "@trade" in stream for stream in streams):
            return WS_PUBLIC_BASE_URL
        return WS_MARKET_BASE_URL

    async def _raw_stream(self, streams: list[str], emit_reconnect: bool = False) -> AsyncIterator[tuple[Any, str]]:
        if not streams:
            return
        base_url = self._ws_base_url(streams)
        url = f"{base_url}?streams={'/'.join(streams)}"
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
                    logger.warning("Binance combined stream closed, reconnecting", extra={"aitos_extra": {"streams": streams, "backoff_seconds": backoff}})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Binance combined stream disconnected, reconnecting", extra={"aitos_extra": {"streams": streams, "error": str(exc), "backoff_seconds": backoff}})
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
