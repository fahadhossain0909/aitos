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
    BINANCE_USDM_WS_RAW,
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
WS_PUBLIC_BASE_URL = WS_MARKET_BASE_URL
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
BINANCE_MAX_STREAMS_PER_CONNECTION = 900


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
        if not symbols:
            return
        normalized = list(dict.fromkeys(s.upper() for s in symbols))
        primary = self._raw_stream(
            [f"{s.lower()}@aggTrade" for s in normalized], emit_reconnect=True
        ).__aiter__()
        fallback = {
            s: self._direct_raw_stream(
                f"{s.lower()}@aggTrade", emit_reconnect=True
            ).__aiter__()
            for s in normalized
        }
        loop = asyncio.get_running_loop()
        last_primary = {s: loop.time() for s in normalized}
        active: set[str] = set()
        primary_task: asyncio.Task | None = asyncio.create_task(primary.__anext__())
        fallback_tasks: dict[str, asyncio.Task | None] = {s: None for s in normalized}
        try:
            while True:
                now = loop.time()
                for symbol in normalized:
                    if (
                        symbol not in active
                        and now - last_primary[symbol]
                        >= TRADE_STREAM_IDLE_FALLBACK_SECONDS
                    ):
                        active.add(symbol)
                        fallback_tasks[symbol] = asyncio.create_task(
                            fallback[symbol].__anext__()
                        )
                tasks = {primary_task} if primary_task is not None else set()
                tasks.update(t for t in fallback_tasks.values() if t is not None)
                if not tasks:
                    return
                timeout = TRADE_STREAM_PRIMARY_RETRY_SECONDS
                for symbol in normalized:
                    if symbol not in active:
                        timeout = min(
                            timeout,
                            max(
                                0.0,
                                TRADE_STREAM_IDLE_FALLBACK_SECONDS
                                - (loop.time() - last_primary[symbol]),
                            ),
                        )
                done, _ = await asyncio.wait(
                    tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    continue
                if primary_task is not None and primary_task in done:
                    task, primary_task = primary_task, None
                    try:
                        data, stream_name = task.result()
                        symbol = stream_name.split("@", 1)[0].upper()
                        if symbol in normalized:
                            last_primary[symbol] = loop.time()
                            if symbol in active:
                                active.discard(symbol)
                                ft = fallback_tasks[symbol]
                                fallback_tasks[symbol] = None
                                if ft is not None and not ft.done():
                                    ft.cancel()
                                    await asyncio.gather(ft, return_exceptions=True)
                            yield parse_agg_trade_ws(data)
                    except StopAsyncIteration:
                        pass
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.error(
                            "Binance combined aggregate-trade stream event failed",
                            extra={"aitos_extra": {"error": str(exc)}},
                        )
                for symbol in normalized:
                    task = fallback_tasks[symbol]
                    if task is None or task not in done or symbol not in active:
                        continue
                    fallback_tasks[symbol] = None
                    try:
                        data, _ = task.result()
                        yield parse_agg_trade_ws(data)
                    except StopAsyncIteration:
                        pass
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.error(
                            "Binance direct aggTrade fallback event invalid",
                            extra={
                                "aitos_extra": {"symbol": symbol, "error": str(exc)}
                            },
                        )
                    if symbol in active:
                        fallback_tasks[symbol] = asyncio.create_task(
                            fallback[symbol].__anext__()
                        )
                if primary_task is None:
                    primary_task = asyncio.create_task(primary.__anext__())
        finally:
            if primary_task is not None and not primary_task.done():
                primary_task.cancel()
            await asyncio.gather(
                *(t for t in (primary_task,) if t is not None), return_exceptions=True
            )
            for task in fallback_tasks.values():
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(t for t in fallback_tasks.values() if t is not None),
                return_exceptions=True,
            )
            await primary.aclose()
            for iterator in fallback.values():
                await iterator.aclose()

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
        return WS_PUBLIC_BASE_URL

    async def _direct_raw_stream(
        self, stream: str, emit_reconnect: bool = False
    ) -> AsyncIterator[tuple[Any, str]]:
        async for item in self._connect_raw(
            f"{WS_MARKET_RAW_BASE_URL}/{stream}", stream, emit_reconnect
        ):
            yield item

    async def _raw_stream(
        self, streams: list[str], emit_reconnect: bool = False
    ) -> AsyncIterator[tuple[Any, str]]:
        if not streams:
            return
        shards = self._partition_streams(streams)
        if len(shards) == 1:
            url = f"{self._ws_base_url(shards[0])}?streams={'/'.join(shards[0])}"
            async for item in self._connect_raw(url, None, emit_reconnect, shards[0]):
                yield item
            return

        # Binance permits at most 1024 streams per connection. Keep a safety
        # margin and merge deterministic shards so all-market universes can scale
        # without silently exceeding the venue limit.
        iterators = [
            self._connect_raw(
                f"{self._ws_base_url(shard)}?streams={'/'.join(shard)}",
                None,
                emit_reconnect,
                shard,
            ).__aiter__()
            for shard in shards
        ]
        tasks: dict[asyncio.Task, int] = {
            asyncio.create_task(iterator.__anext__()): index
            for index, iterator in enumerate(iterators)
        }
        try:
            while tasks:
                done, _ = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
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
                                "aitos_extra": {
                                    "shard_index": index,
                                    "error": str(exc),
                                }
                            },
                        )
                    tasks[asyncio.create_task(iterators[index].__anext__())] = index
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(
                *(iterator.aclose() for iterator in iterators),
                return_exceptions=True,
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
                async with self._ws_connector(url) as ws:
                    backoff = INITIAL_BACKOFF_SECONDS
                    # Binance documents a hard 24h connection lifetime. Recycle
                    # before that limit so the exchange does not choose the cutover.
                    async with asyncio.timeout(BINANCE_USDM_WS_MAX_LIFETIME_SECONDS):
                        async for raw_message in ws:
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
