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
        raw = await self._get(
            "/fapi/v1/depth", {"symbol": symbol, "limit": limit}, weight
        )
        return parse_order_book_rest(raw, symbol=symbol)

    async def fetch_recent_trades(
        self, symbol: str, limit: int = 500
    ) -> list[TradeTick]:
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
        self, symbols: list[str] | None = None
    ) -> dict[str, SymbolFilters]:
        raw = await self._get("/fapi/v1/exchangeInfo", {}, weight=1)
        all_filters = parse_exchange_info(raw)
        return (
            all_filters
            if symbols is None
            else {s: all_filters[s] for s in symbols if s in all_filters}
        )

    async def stream_klines(
        self, symbols: list[str], timeframe: str
    ) -> AsyncIterator[Kline]:
        streams = [f"{s.lower()}@kline_{timeframe}" for s in symbols]

        async def _parse(data: Any) -> Kline:
            return parse_kline_ws(data)

        async for kline in self._stream(streams, _parse):
            yield kline

    async def stream_trades(self, symbols: list[str]) -> AsyncIterator[TradeTick]:
        """Keep one combined aggTrade primary stream with per-symbol fallback."""
        if not symbols:
            return

        normalized_symbols = list(dict.fromkeys(symbols))
        symbol_set = set(normalized_symbols)
        primary_streams = [
            f"{symbol.lower()}@aggTrade" for symbol in normalized_symbols
        ]
        primary = self._raw_stream(primary_streams, emit_reconnect=True).__aiter__()
        fallback_streams = {
            symbol: self._raw_stream(
                [f"{symbol.lower()}@trade"], emit_reconnect=True
            ).__aiter__()
            for symbol in normalized_symbols
        }
        loop = asyncio.get_running_loop()
        # None means that this symbol has not yet received a valid primary event.
        # The idle watchdog must not start until the first valid aggTrade arrives.
        primary_last_data: dict[str, float | None] = {
            symbol: None for symbol in normalized_symbols
        }
        fallback_active: set[str] = set()
        fallback_tasks: dict[str, asyncio.Task | None] = {
            symbol: None for symbol in normalized_symbols
        }
        primary_task: asyncio.Task | None = None

        def stream_symbol(stream_name: str, data: Any) -> str:
            raw_symbol = stream_name.split("@", 1)[0].upper()
            if raw_symbol in symbol_set:
                return raw_symbol
            symbol = str(data.get("s", "")).upper() if isinstance(data, dict) else ""
            return symbol

        try:
            primary_task = asyncio.create_task(primary.__anext__())
            while True:
                if primary_task is None:
                    primary_task = asyncio.create_task(primary.__anext__())

                now = loop.time()
                for symbol in normalized_symbols:
                    last_data = primary_last_data[symbol]
                    if (
                        symbol not in fallback_active
                        and last_data is not None
                        and now - last_data >= TRADE_STREAM_IDLE_FALLBACK_SECONDS
                    ):
                        fallback_active.add(symbol)
                        fallback_tasks[symbol] = asyncio.create_task(
                            fallback_streams[symbol].__anext__()
                        )
                        logger.warning(
                            "Binance aggregate-trade stream idle; entering per-symbol raw-trade fallback",
                            extra={
                                "aitos_extra": {
                                    "symbol": symbol,
                                    "idle_seconds": TRADE_STREAM_IDLE_FALLBACK_SECONDS,
                                }
                            },
                        )

                tasks: set[asyncio.Task] = {primary_task}
                tasks.update(
                    task for task in fallback_tasks.values() if task is not None
                )

                timeout = TRADE_STREAM_PRIMARY_RETRY_SECONDS
                for symbol in normalized_symbols:
                    last_data = primary_last_data[symbol]
                    if symbol not in fallback_active and last_data is not None:
                        remaining = max(
                            0.0,
                            TRADE_STREAM_IDLE_FALLBACK_SECONDS
                            - (loop.time() - last_data),
                        )
                        timeout = min(timeout, remaining)

                done, _ = await asyncio.wait(
                    tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    continue

                if primary_task in done:
                    try:
                        data, stream_name = primary_task.result()
                        primary_task = None
                        symbol = stream_symbol(stream_name, data)
                        if symbol not in symbol_set:
                            continue
                        try:
                            tick = parse_agg_trade_ws(data)
                        except Exception as exc:
                            logger.error(
                                "Binance aggregate-trade event invalid; keeping primary under watchdog",
                                extra={
                                    "aitos_extra": {"symbol": symbol, "error": str(exc)}
                                },
                            )
                            continue
                        primary_last_data[symbol] = loop.time()
                        if symbol in fallback_active:
                            fallback_active.discard(symbol)
                            fallback_task = fallback_tasks[symbol]
                            fallback_tasks[symbol] = None
                            if fallback_task is not None and not fallback_task.done():
                                fallback_task.cancel()
                                await asyncio.gather(
                                    fallback_task, return_exceptions=True
                                )
                            logger.info(
                                "Binance aggregate-trade stream recovered; returning from per-symbol fallback",
                                extra={"aitos_extra": {"symbol": symbol}},
                            )
                        yield tick
                    except StopAsyncIteration:
                        primary_task = None
                    except Exception as exc:
                        primary_task = None
                        logger.error(
                            "Binance combined aggregate-trade stream event failed",
                            extra={"aitos_extra": {"error": str(exc)}},
                        )

                for symbol in normalized_symbols:
                    task = fallback_tasks[symbol]
                    if (
                        task is None
                        or task not in done
                        or symbol not in fallback_active
                    ):
                        continue
                    try:
                        data, _ = task.result()
                        fallback_tasks[symbol] = None
                        try:
                            yield parse_trade_ws(data)
                        except Exception as exc:
                            logger.error(
                                "Binance raw-trade fallback event invalid",
                                extra={
                                    "aitos_extra": {"symbol": symbol, "error": str(exc)}
                                },
                            )
                    except StopAsyncIteration:
                        fallback_tasks[symbol] = None
                    except Exception as exc:
                        fallback_tasks[symbol] = None
                        logger.error(
                            "Binance raw-trade fallback stream failed",
                            extra={
                                "aitos_extra": {"symbol": symbol, "error": str(exc)}
                            },
                        )
                    finally:
                        if symbol in fallback_active and fallback_tasks[symbol] is None:
                            fallback_tasks[symbol] = asyncio.create_task(
                                fallback_streams[symbol].__anext__()
                            )
        except asyncio.CancelledError:
            raise
        finally:
            if primary_task is not None and not primary_task.done():
                primary_task.cancel()
            await asyncio.gather(
                *(task for task in [primary_task] if task is not None),
                return_exceptions=True,
            )
            for task in fallback_tasks.values():
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in fallback_tasks.values() if task is not None),
                return_exceptions=True,
            )
            await primary.aclose()
            for iterator in fallback_streams.values():
                await iterator.aclose()

    async def stream_order_book(
        self, symbols: list[str], levels: int = 20
    ) -> AsyncIterator[OrderBookSnapshot]:
        if not symbols:
            return
        streams = [f"{s.lower()}@depth@100ms" for s in symbols]
        symbol_by_stream = {f"{s.lower()}@depth@100ms": s for s in symbols}
        queue: asyncio.Queue[tuple[Any, str]] = asyncio.Queue(
            maxsize=ORDERBOOK_BOOTSTRAP_QUEUE_SIZE
        )
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

        async def bootstrap(symbol: str) -> LocalOrderBook:
            book = LocalOrderBook(symbol=symbol, max_levels=levels)
            snapshot = await self.fetch_order_book(symbol, limit=max(levels, 50))
            book.seed(snapshot)
            return book

        producer_task = asyncio.create_task(
            producer(), name="binance-orderbook-producer"
        )
        books: dict[str, LocalOrderBook] = {}
        try:
            try:
                await asyncio.wait_for(
                    producer_ready.wait(),
                    timeout=ORDERBOOK_BOOTSTRAP_READY_TIMEOUT_SECONDS,
                )
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
                    logger.warning(
                        "order-book sequence break; reseeding from REST",
                        extra={"aitos_extra": {"symbol": symbol}},
                    )
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
        async with self._session.get(
            f"{REST_BASE_URL}{path}", params=params
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def _stream(
        self, streams: list[str], parser: Callable[[Any], Any]
    ) -> AsyncIterator[Any]:
        async for data, _stream_name in self._raw_stream(streams):
            yield await parser(data)

    @staticmethod
    def _ws_base_url(streams: list[str]) -> str:
        if not streams:
            return WS_MARKET_BASE_URL
        if all("@depth" in stream or "@trade" in stream for stream in streams):
            return WS_PUBLIC_BASE_URL
        return WS_MARKET_BASE_URL

    async def _raw_stream(
        self, streams: list[str], emit_reconnect: bool = False
    ) -> AsyncIterator[tuple[Any, str]]:
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
