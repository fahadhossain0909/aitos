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
)
from aitos.exchange.rate_limiter import TokenBucketRateLimiter
from aitos.exchange.symbol_filters import SymbolFilters, parse_exchange_info
from aitos.logging_setup import get_logger
from aitos.market_data.endpoints import (
    BINANCE_USDM_WS_COMBINED,
    BINANCE_USDM_WS_MAX_LIFETIME_SECONDS,
    BINANCE_USDM_WS_PUBLIC_COMBINED,
    BINANCE_USDM_WS_RAW,
    BINANCE_USDM_WS_PUBLIC_RAW,
)
from aitos.models.market import (
    FundingRate,
    Kline,
    OpenInterest,
    OrderBookSnapshot,
    TradeTick,
)

logger = get_logger("aitos.exchange.binance")
REST_BASE_URL = "https://fapi.binance.com"
WS_MARKET_BASE_URL = BINANCE_USDM_WS_COMBINED
WS_MARKET_RAW_BASE_URL = BINANCE_USDM_WS_RAW
WS_PUBLIC_BASE_URL = BINANCE_USDM_WS_PUBLIC_COMBINED
WS_PUBLIC_RAW_BASE_URL = BINANCE_USDM_WS_PUBLIC_RAW
DEFAULT_RATE_LIMIT_CAPACITY = 2000
DEFAULT_RATE_LIMIT_REFILL_PER_SECOND = 2000 / 60
MAX_BACKOFF_SECONDS = 60.0
INITIAL_BACKOFF_SECONDS = 1.0
ORDERBOOK_BOOTSTRAP_QUEUE_SIZE = 5000
ORDERBOOK_BOOTSTRAP_READY_TIMEOUT_SECONDS = 10.0
WS_PING_INTERVAL_SECONDS = 15.0
WS_PING_TIMEOUT_SECONDS = 10.0
WS_OPEN_TIMEOUT_SECONDS = 10.0
# Keep connections well below Binance's documented per-connection stream ceiling.
# More importantly, never create one WebSocket connection per symbol as a
# "fallback": with hundreds of symbols that can exhaust the venue/IP connection
# budget and make a genuine network problem look like total market-data failure.
BINANCE_MAX_STREAMS_PER_CONNECTION = 200


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

            ws_connector = lambda url: websockets.connect(
                url,
                ping_interval=WS_PING_INTERVAL_SECONDS,
                ping_timeout=WS_PING_TIMEOUT_SECONDS,
                open_timeout=WS_OPEN_TIMEOUT_SECONDS,
            )
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
    ) -> list[Kline]:
        weight = 5 if limit <= 100 else (10 if limit <= 500 else 25)
        raw = await self._get(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": timeframe, "limit": limit},
            weight,
        )
        return [
            parse_kline_rest(row, symbol=symbol, timeframe=timeframe) for row in raw
        ]

    async def fetch_order_book(self, symbol: str, limit: int = 50) -> OrderBookSnapshot:
        weight = 2 if limit <= 50 else (5 if limit <= 100 else 10)
        return parse_order_book_rest(
            await self._get(
                "/fapi/v1/depth", {"symbol": symbol, "limit": limit}, weight
            ),
            symbol=symbol,
        )

    async def fetch_recent_trades(
        self, symbol: str, limit: int = 500
    ) -> list[TradeTick]:
        raw = await self._get("/fapi/v1/trades", {"symbol": symbol, "limit": limit}, 5)
        return [parse_trade_rest(row, symbol=symbol) for row in raw]

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        return parse_funding_rate_rest(
            await self._get("/fapi/v1/premiumIndex", {"symbol": symbol}, 1)
        )

    async def fetch_open_interest(self, symbol: str) -> OpenInterest:
        return parse_open_interest_rest(
            await self._get("/fapi/v1/openInterest", {"symbol": symbol}, 1)
        )

    async def fetch_exchange_info(
        self, symbols: list[str] | None = None
    ) -> dict[str, SymbolFilters]:
        all_filters = parse_exchange_info(
            await self._get("/fapi/v1/exchangeInfo", {}, 1)
        )
        return (
            all_filters
            if symbols is None
            else {s: all_filters[s] for s in symbols if s in all_filters}
        )

    async def stream_klines(
        self, symbols: list[str], timeframe: str
    ) -> AsyncIterator[Kline]:
        streams = [f"{s.lower()}@kline_{timeframe}" for s in dict.fromkeys(symbols)]
        async for data, _ in self._raw_stream(streams, emit_reconnect=True):
            yield parse_kline_ws(data)

    async def stream_trades(self, symbols: list[str]) -> AsyncIterator[TradeTick]:
        """Yield aggregate trades from bounded combined-stream shards."""
        normalized = list(dict.fromkeys(s.upper() for s in symbols))
        if not normalized:
            return
        streams = [f"{s.lower()}@aggTrade" for s in normalized]
        async for data, _ in self._raw_stream(streams, emit_reconnect=True):
            yield parse_agg_trade_ws(data)

    async def stream_order_book(
        self, symbols: list[str], levels: int = 20
    ) -> AsyncIterator[OrderBookSnapshot]:
        if not symbols:
            return
        streams = [f"{s.lower()}@depth@100ms" for s in dict.fromkeys(symbols)]
        symbol_by_stream = {f"{s.lower()}@depth@100ms": s for s in symbols}
        queue: asyncio.Queue[tuple[Any, str]] = asyncio.Queue(
            maxsize=ORDERBOOK_BOOTSTRAP_QUEUE_SIZE
        )
        ready = asyncio.Event()

        async def producer() -> None:
            try:
                async for data, stream_name in self._raw_stream(
                    streams, emit_reconnect=True
                ):
                    ready.set()
                    try:
                        queue.put_nowait((data, stream_name))
                    except asyncio.QueueFull:
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
            book.seed(await self.fetch_order_book(symbol, limit=max(levels, 50)))
            return book

        producer_task = asyncio.create_task(producer())
        books: dict[str, LocalOrderBook] = {}
        try:
            await asyncio.wait_for(
                ready.wait(), timeout=ORDERBOOK_BOOTSTRAP_READY_TIMEOUT_SECONDS
            )
            for symbol in symbols:
                books[symbol] = await bootstrap(symbol)
            while True:
                data, stream_name = await queue.get()
                symbol = symbol_by_stream.get(stream_name)
                if symbol is None:
                    continue
                try:
                    snapshot = books[symbol].apply(parse_depth_diff_ws(data))
                except OrderBookSequenceError:
                    books[symbol] = await bootstrap(symbol)
                    continue
                if snapshot is not None:
                    yield snapshot
        finally:
            producer_task.cancel()
            await asyncio.gather(producer_task, return_exceptions=True)

    async def stream_order_books(
        self, symbols: list[str], levels: int = 20
    ) -> AsyncIterator[OrderBookSnapshot]:
        async for snapshot in self.stream_order_book(symbols, levels):
            yield snapshot

    async def stream_order_book_deltas(
        self, symbols: list[str]
    ) -> AsyncIterator[tuple[str, Any]]:
        if not symbols:
            return
        streams = [f"{s.lower()}@depth@100ms" for s in dict.fromkeys(symbols)]
        async for data, stream_name in self._raw_stream(streams, emit_reconnect=True):
            symbol = stream_name.split("@", 1)[0].upper()
            try:
                yield symbol, parse_depth_diff_ws(data)
            except Exception as exc:
                logger.error(
                    "Binance depth delta invalid",
                    extra={"aitos_extra": {"symbol": symbol, "error": str(exc)}},
                )

    @staticmethod
    def _partition_streams(
        streams: list[str], max_per_connection: int = BINANCE_MAX_STREAMS_PER_CONNECTION
    ) -> list[list[str]]:
        if max_per_connection <= 0:
            raise ValueError("max_per_connection must be positive")
        unique = list(dict.fromkeys(streams))
        return [
            unique[i : i + max_per_connection]
            for i in range(0, len(unique), max_per_connection)
        ]

    @staticmethod
    def _get_ws_base_url(streams: list[str]) -> tuple[str, str]:
        """Select Binance's market vs public routing by stream family."""
        if any("@depth" in stream or "@bookTicker" in stream for stream in streams):
            return WS_PUBLIC_BASE_URL, WS_PUBLIC_RAW_BASE_URL
        return WS_MARKET_BASE_URL, WS_MARKET_RAW_BASE_URL

    async def _get(self, path: str, params: dict[str, Any], weight: int) -> Any:
        await self._rate_limiter.acquire(weight)
        await self.connect()
        assert self._session is not None
        async with self._session.get(
            f"{REST_BASE_URL}{path}", params=params
        ) as response:
            response.raise_for_status()
            return await response.json()

    @staticmethod
    def _ws_base_url(streams: list[str]) -> str:
        return BinanceFuturesAdapter._get_ws_base_url(streams)[0]

    async def _raw_stream(
        self, streams: list[str], emit_reconnect: bool = False
    ) -> AsyncIterator[tuple[Any, str]]:
        if not streams:
            return
        shards = self._partition_streams(streams)
        if len(shards) == 1:
            base_url, _ = self._get_ws_base_url(shards[0])
            url = f"{base_url}?streams={'/'.join(shards[0])}"
            async for item in self._connect_raw(url, None, emit_reconnect, shards[0]):
                yield item
            return

        iterators = []
        for shard in shards:
            base_url, _ = self._get_ws_base_url(shard)
            iterators.append(
                self._connect_raw(
                    f"{base_url}?streams={'/'.join(shard)}",
                    None,
                    emit_reconnect,
                    shard,
                ).__aiter__()
            )
        tasks: dict[asyncio.Task, int] = {
            asyncio.create_task(iterator.__anext__()): index
            for index, iterator in enumerate(iterators)
        }
        try:
            while tasks:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    index = tasks.pop(task)
                    try:
                        yield task.result()
                    except StopAsyncIteration:
                        continue
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.error(
                            "Binance sharded websocket iterator failed",
                            extra={
                                "aitos_extra": {"shard_index": index, "error": str(exc)}
                            },
                        )
                    else:
                        tasks[asyncio.create_task(iterators[index].__anext__())] = index
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(
                *(iterator.aclose() for iterator in iterators), return_exceptions=True
            )

    async def _connect_raw(
        self,
        url: str,
        direct_stream: str | None,
        emit_reconnect: bool,
        streams: list[str] | None = None,
    ) -> AsyncIterator[tuple[Any, str]]:
        backoff = INITIAL_BACKOFF_SECONDS
        while True:
            try:
                logger.info(
                    "Binance websocket connecting",
                    extra={
                        "aitos_extra": {
                            "url": url,
                            "streams": streams or [direct_stream],
                        }
                    },
                )
                async with self._ws_connector(url) as ws:
                    logger.info(
                        "Binance websocket connected",
                        extra={"aitos_extra": {"url": url}},
                    )
                    backoff = INITIAL_BACKOFF_SECONDS
                    first_message = True
                    async with asyncio.timeout(BINANCE_USDM_WS_MAX_LIFETIME_SECONDS):
                        async for raw_message in ws:
                            if first_message:
                                logger.info(
                                    "Binance websocket first message received",
                                    extra={"aitos_extra": {"url": url}},
                                )
                                first_message = False
                            envelope = json.loads(raw_message)
                            if direct_stream is not None:
                                yield envelope, direct_stream
                            else:
                                yield envelope.get("data", envelope), envelope.get(
                                    "stream", ""
                                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                logger.info(
                    "Binance websocket reached proactive lifecycle rotation",
                    extra={"aitos_extra": {"url": url}},
                )
            except Exception as exc:
                logger.error(
                    "Binance websocket disconnected; reconnecting",
                    extra={
                        "aitos_extra": {
                            "url": url,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "backoff_seconds": backoff,
                        }
                    },
                )
            if emit_reconnect:
                logger.warning(
                    "Binance market stream reconnecting",
                    extra={
                        "aitos_extra": {
                            "streams": streams or [direct_stream],
                            "backoff_seconds": backoff,
                        }
                    },
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    async def __aenter__(self) -> BinanceFuturesAdapter:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()
