"""Binance USDT-M Futures exchange adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
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
WS_PUBLIC_RAW_BASE_URL = WS_MARKET_RAW_BASE_URL
DEFAULT_RATE_LIMIT_CAPACITY = 2000
DEFAULT_RATE_LIMIT_REFILL_PER_SECOND = 2000 / 60
MAX_BACKOFF_SECONDS = 60.0
INITIAL_BACKOFF_SECONDS = 1.0
ORDERBOOK_BOOTSTRAP_QUEUE_SIZE = 5000
ORDERBOOK_BOOTSTRAP_READY_TIMEOUT_SECONDS = 10.0
WS_PING_INTERVAL_SECONDS = 15.0
WS_PING_TIMEOUT_SECONDS = 10.0
WS_OPEN_TIMEOUT_SECONDS = 10.0
BINANCE_MAX_STREAMS_PER_CONNECTION = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BinanceFuturesAdapter(ExchangeAdapter):
    def __init__(
        self,
        session_factory: Callable[[], aiohttp.ClientSession] = aiohttp.ClientSession,
        ws_connector: Callable[..., Any] | None = None,
        rate_limiter: TokenBucketRateLimiter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._session: aiohttp.ClientSession | None = None
        self._ws_transport: dict[str, Any] = {
            "state": "idle",
            "current_url": None,
            "streams": [],
            "connect_attempts": 0,
            "successful_handshakes": 0,
            "frames_received": 0,
            "market_events_received": 0,
            "close_count": 0,
            "subscription_mode": "combined_url",
            "last_connect_started_at": None,
            "last_handshake_at": None,
            "last_first_frame_at": None,
            "last_market_event_at": None,
            "last_close_at": None,
            "last_close_code": None,
            "last_close_reason": None,
            "last_error_type": None,
            "last_error": None,
        }
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

    def websocket_transport_snapshot(self) -> dict[str, Any]:
        """Return a copy of transport-stage telemetry safe for health reports."""
        snapshot = dict(self._ws_transport)
        snapshot["streams"] = list(self._ws_transport.get("streams", []))
        return snapshot

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
        """Route all Binance USDⓈ-M public streams through the market path."""
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
            ws = None
            try:
                started_at = _now_iso()
                self._ws_transport.update(
                    {
                        "state": "connecting",
                        "current_url": url,
                        "streams": list(
                            streams or ([direct_stream] if direct_stream else [])
                        ),
                        "connect_attempts": self._ws_transport["connect_attempts"] + 1,
                        "last_connect_started_at": started_at,
                        "last_error_type": None,
                        "last_error": None,
                    }
                )
                logger.info(
                    "Binance websocket connecting",
                    extra={
                        "aitos_extra": {
                            "stage": "connect_started",
                            "url": url,
                            "streams": streams or [direct_stream],
                        }
                    },
                )
                async with self._ws_connector(url) as ws:
                    handshake_at = _now_iso()
                    self._ws_transport.update(
                        {
                            "state": "connected",
                            "successful_handshakes": self._ws_transport[
                                "successful_handshakes"
                            ]
                            + 1,
                            "last_handshake_at": handshake_at,
                        }
                    )
                    logger.info(
                        "Binance websocket connected",
                        extra={
                            "aitos_extra": {
                                "stage": "handshake_complete",
                                "url": url,
                                "subscription_mode": "combined_url",
                            }
                        },
                    )
                    logger.info(
                        "Binance websocket subscription active",
                        extra={
                            "aitos_extra": {
                                "stage": "subscription_active",
                                "mode": "combined_url",
                                "streams": streams or [direct_stream],
                            }
                        },
                    )
                    backoff = INITIAL_BACKOFF_SECONDS
                    first_message = True
                    async with asyncio.timeout(BINANCE_USDM_WS_MAX_LIFETIME_SECONDS):
                        while True:
                            raw_message = await ws.recv()
                            self._ws_transport["frames_received"] += 1
                            if first_message:
                                first_at = _now_iso()
                                self._ws_transport["last_first_frame_at"] = first_at
                                logger.info(
                                    "Binance websocket first frame received",
                                    extra={
                                        "aitos_extra": {
                                            "stage": "first_frame",
                                            "url": url,
                                        }
                                    },
                                )
                                first_message = False
                            envelope = json.loads(raw_message)
                            if direct_stream is not None:
                                payload, stream_name = envelope, direct_stream
                            else:
                                payload, stream_name = envelope.get(
                                    "data", envelope
                                ), envelope.get("stream", "")
                            self._ws_transport["market_events_received"] += 1
                            self._ws_transport["last_market_event_at"] = _now_iso()
                            yield payload, stream_name
            except asyncio.CancelledError:
                raise
            except StopAsyncIteration:
                self._ws_transport.update(
                    {
                        "state": "closed",
                        "last_close_at": _now_iso(),
                        "last_close_code": getattr(ws, "close_code", None),
                        "last_close_reason": getattr(ws, "close_reason", None),
                    }
                )
                return
            except TimeoutError:
                self._ws_transport.update(
                    {
                        "state": "lifecycle_rotation",
                        "last_close_at": _now_iso(),
                        "last_close_code": getattr(ws, "close_code", None),
                        "last_close_reason": getattr(ws, "close_reason", None),
                    }
                )
                logger.info(
                    "Binance websocket reached proactive lifecycle rotation",
                    extra={
                        "aitos_extra": {
                            "stage": "lifecycle_rotation",
                            "url": url,
                            "close_code": getattr(ws, "close_code", None),
                            "close_reason": getattr(ws, "close_reason", None),
                        }
                    },
                )
            except Exception as exc:
                close_code = getattr(ws, "close_code", None)
                close_reason = getattr(ws, "close_reason", None)
                self._ws_transport.update(
                    {
                        "state": "reconnecting",
                        "close_count": self._ws_transport["close_count"] + 1,
                        "last_close_at": _now_iso(),
                        "last_close_code": close_code,
                        "last_close_reason": close_reason,
                        "last_error_type": type(exc).__name__,
                        "last_error": str(exc)[:500],
                    }
                )
                logger.error(
                    "Binance websocket disconnected; reconnecting",
                    extra={
                        "aitos_extra": {
                            "stage": "disconnect",
                            "url": url,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "close_code": close_code,
                            "close_reason": close_reason,
                            "backoff_seconds": backoff,
                        }
                    },
                )
            else:
                close_code = getattr(ws, "close_code", None)
                close_reason = getattr(ws, "close_reason", None)
                self._ws_transport.update(
                    {
                        "state": "reconnecting",
                        "close_count": self._ws_transport["close_count"] + 1,
                        "last_close_at": _now_iso(),
                        "last_close_code": close_code,
                        "last_close_reason": close_reason,
                    }
                )
                logger.warning(
                    "Binance websocket stream ended; reconnecting",
                    extra={
                        "aitos_extra": {
                            "stage": "stream_end",
                            "url": url,
                            "close_code": close_code,
                            "close_reason": close_reason,
                        }
                    },
                )
            if emit_reconnect:
                logger.warning(
                    "Binance market stream reconnecting",
                    extra={
                        "aitos_extra": {
                            "stage": "reconnect_wait",
                            "streams": streams or [direct_stream],
                            "backoff_seconds": backoff,
                        }
                    },
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    async def _connect_raw_dynamic(
        self,
        get_streams: Callable[[], list[str]],
        changed: asyncio.Event,
        emit_reconnect: bool,
        label: str,
    ) -> AsyncIterator[tuple[Any, str]]:
        """Like ``_connect_raw``, but the subscribed stream set can change
        while the connection stays open.

        On every (re)connect, subscribes to whatever ``get_streams()``
        currently returns. While connected, whenever ``changed`` is set, the
        delta versus what is currently subscribed is sent as Binance
        SUBSCRIBE/UNSUBSCRIBE control frames over the *same* websocket — no
        reconnect, no orderbook re-bootstrap, no freshness gap. A reconnect
        only happens on genuine connection loss or the Binance-enforced
        lifecycle rotation, exactly as in ``_connect_raw``.
        """
        backoff = INITIAL_BACKOFF_SECONDS
        while True:
            streams = list(dict.fromkeys(get_streams()))
            if not streams:
                changed.clear()
                await changed.wait()
                continue
            base_url, _ = self._get_ws_base_url(streams)
            url = f"{base_url}?streams={'/'.join(streams)}"
            subscribed = set(streams)
            ws = None
            recv_task: asyncio.Task | None = None
            try:
                started_at = _now_iso()
                self._ws_transport.update(
                    {
                        "state": "connecting",
                        "current_url": url,
                        "streams": streams,
                        "connect_attempts": self._ws_transport["connect_attempts"] + 1,
                        "last_connect_started_at": started_at,
                        "last_error_type": None,
                        "last_error": None,
                    }
                )
                logger.info(
                    "Binance managed websocket connecting",
                    extra={"aitos_extra": {"stage": "connect_started", "url": url, "label": label}},
                )
                async with self._ws_connector(url) as ws:
                    handshake_at = _now_iso()
                    self._ws_transport.update(
                        {
                            "state": "connected",
                            "successful_handshakes": self._ws_transport[
                                "successful_handshakes"
                            ]
                            + 1,
                            "last_handshake_at": handshake_at,
                        }
                    )
                    logger.info(
                        "Binance managed websocket connected",
                        extra={
                            "aitos_extra": {
                                "stage": "handshake_complete",
                                "url": url,
                                "subscription_mode": "combined_url+dynamic",
                                "label": label,
                            }
                        },
                    )
                    backoff = INITIAL_BACKOFF_SECONDS
                    first_message = True
                    sub_request_id = 0
                    async with asyncio.timeout(BINANCE_USDM_WS_MAX_LIFETIME_SECONDS):
                        recv_task = asyncio.ensure_future(ws.recv())
                        try:
                            while True:
                                change_task = asyncio.ensure_future(changed.wait())
                                done, _pending = await asyncio.wait(
                                    {recv_task, change_task},
                                    return_when=asyncio.FIRST_COMPLETED,
                                )
                                if change_task in done:
                                    changed.clear()
                                    desired = set(dict.fromkeys(get_streams()))
                                    to_add = sorted(desired - subscribed)
                                    to_remove = sorted(subscribed - desired)
                                    if to_add:
                                        sub_request_id += 1
                                        await ws.send(
                                            json.dumps(
                                                {
                                                    "method": "SUBSCRIBE",
                                                    "params": to_add,
                                                    "id": sub_request_id,
                                                }
                                            )
                                        )
                                    if to_remove:
                                        sub_request_id += 1
                                        await ws.send(
                                            json.dumps(
                                                {
                                                    "method": "UNSUBSCRIBE",
                                                    "params": to_remove,
                                                    "id": sub_request_id,
                                                }
                                            )
                                        )
                                    if to_add or to_remove:
                                        logger.info(
                                            "Binance managed subscription updated",
                                            extra={
                                                "aitos_extra": {
                                                    "label": label,
                                                    "added": to_add,
                                                    "removed": to_remove,
                                                    "total": len(desired),
                                                }
                                            },
                                        )
                                    subscribed = desired
                                    self._ws_transport["streams"] = sorted(subscribed)
                                else:
                                    change_task.cancel()
                                if recv_task in done:
                                    raw_message = recv_task.result()
                                    self._ws_transport["frames_received"] += 1
                                    if first_message:
                                        self._ws_transport["last_first_frame_at"] = (
                                            _now_iso()
                                        )
                                        first_message = False
                                    envelope = json.loads(raw_message)
                                    payload, stream_name = envelope.get(
                                        "data", envelope
                                    ), envelope.get("stream", "")
                                    self._ws_transport["market_events_received"] += 1
                                    self._ws_transport["last_market_event_at"] = (
                                        _now_iso()
                                    )
                                    recv_task = asyncio.ensure_future(ws.recv())
                                    yield payload, stream_name
                        finally:
                            for pending_task in (recv_task, change_task):
                                if pending_task is not None and not pending_task.done():
                                    pending_task.cancel()
                            await asyncio.gather(
                                *(
                                    t
                                    for t in (recv_task, change_task)
                                    if t is not None
                                ),
                                return_exceptions=True,
                            )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self._ws_transport.update(
                    {
                        "state": "lifecycle_rotation",
                        "last_close_at": _now_iso(),
                        "last_close_code": getattr(ws, "close_code", None),
                        "last_close_reason": getattr(ws, "close_reason", None),
                    }
                )
                logger.info(
                    "Binance managed websocket reached proactive lifecycle rotation",
                    extra={"aitos_extra": {"stage": "lifecycle_rotation", "url": url, "label": label}},
                )
            except Exception as exc:
                close_code = getattr(ws, "close_code", None)
                close_reason = getattr(ws, "close_reason", None)
                self._ws_transport.update(
                    {
                        "state": "reconnecting",
                        "close_count": self._ws_transport["close_count"] + 1,
                        "last_close_at": _now_iso(),
                        "last_close_code": close_code,
                        "last_close_reason": close_reason,
                        "last_error_type": type(exc).__name__,
                        "last_error": str(exc)[:500],
                    }
                )
                logger.error(
                    "Binance managed websocket disconnected; reconnecting",
                    extra={
                        "aitos_extra": {
                            "stage": "disconnect",
                            "url": url,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "close_code": close_code,
                            "close_reason": close_reason,
                            "backoff_seconds": backoff,
                            "label": label,
                        }
                    },
                )
            if emit_reconnect:
                logger.warning(
                    "Binance managed market stream reconnecting",
                    extra={
                        "aitos_extra": {
                            "stage": "reconnect_wait",
                            "streams": streams,
                            "backoff_seconds": backoff,
                            "label": label,
                        }
                    },
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def stream_trades_managed(self, symbols: list[str]) -> "ManagedStreamSet":
        """Long-lived trade subscription; update via ``update_symbols()``.

        Unlike ``stream_trades``, changing the symbol set does not cancel
        or reconnect the underlying websocket — it sends an incremental
        SUBSCRIBE/UNSUBSCRIBE control frame on the live connection.
        """

        def to_stream(symbol: str) -> str:
            return f"{symbol.lower()}@aggTrade"

        streams = [to_stream(s) for s in dict.fromkeys(s.upper() for s in symbols)]
        return ManagedStreamSet(self, "trades", streams, to_stream=to_stream)

    def stream_klines_managed(
        self, symbols: list[str], timeframe: str
    ) -> "ManagedStreamSet":
        """Long-lived kline subscription; update via ``update_symbols()``."""

        def to_stream(symbol: str) -> str:
            return f"{symbol.lower()}@kline_{timeframe}"

        streams = [to_stream(s) for s in dict.fromkeys(s.upper() for s in symbols)]
        return ManagedStreamSet(self, "klines", streams, to_stream=to_stream)

    def stream_order_book_managed(
        self, symbols: list[str], levels: int = 20
    ) -> "ManagedOrderBookStream":
        """Long-lived orderbook subscription; update via ``update_symbols()``.

        Existing per-symbol ``LocalOrderBook`` state is preserved across
        symbol-set updates — only newly-added symbols get REST-bootstrapped,
        instead of every symbol being re-bootstrapped on every change.
        """
        return ManagedOrderBookStream(self, symbols, levels)

    async def __aenter__(self) -> BinanceFuturesAdapter:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()


MANAGED_STREAM_QUEUE_SIZE = 20000


class ManagedStreamSet:
    """A single long-lived Binance combined-stream subscription that can be
    updated live (``update_symbols`` / ``update_streams``) without the
    caller having to cancel and recreate a task.

    Only a genuine connection loss or Binance's own lifecycle rotation
    causes a reconnect; ordinary symbol-set churn (a position opening or
    closing, the scanner's ranking shifting) is handled with
    SUBSCRIBE/UNSUBSCRIBE control frames on the existing websocket.
    """

    def __init__(
        self,
        adapter: BinanceFuturesAdapter,
        kind: str,
        initial_streams: list[str],
        to_stream: Callable[[str], str] | None = None,
    ) -> None:
        self._adapter = adapter
        self._kind = kind
        self._to_stream = to_stream
        self._streams: list[str] = list(dict.fromkeys(initial_streams))
        self._changed = asyncio.Event()
        self._queue: asyncio.Queue[tuple[Any, str]] = asyncio.Queue(
            maxsize=MANAGED_STREAM_QUEUE_SIZE
        )
        self._closed = False
        self._task = asyncio.create_task(
            self._pump(), name=f"aitos-managed-stream-{kind}"
        )

    def _get_streams(self) -> list[str]:
        return list(self._streams)

    async def _pump(self) -> None:
        async for data, stream_name in self._adapter._connect_raw_dynamic(
            self._get_streams, self._changed, True, self._kind
        ):
            try:
                self._queue.put_nowait((data, stream_name))
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self._queue.put_nowait((data, stream_name))

    async def update_streams(self, streams: list[str]) -> bool:
        normalized = list(dict.fromkeys(streams))
        if normalized == self._streams:
            return False
        self._streams = normalized
        self._changed.set()
        return True

    async def update_symbols(self, symbols: list[str]) -> bool:
        if self._to_stream is None:
            raise RuntimeError(
                f"ManagedStreamSet({self._kind}) was not created with a "
                "symbol->stream converter; use update_streams() instead"
            )
        streams = [
            self._to_stream(s) for s in dict.fromkeys(s.upper() for s in symbols)
        ]
        return await self.update_streams(streams)

    def __aiter__(self) -> "ManagedStreamSet":
        return self

    async def __anext__(self) -> tuple[Any, str]:
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)


class ManagedOrderBookStream:
    """Incrementally-updatable local order-book stream.

    ``LocalOrderBook`` state for symbols that stay subscribed survives
    symbol-set updates — only newly-added symbols are REST-bootstrapped.
    Only a real websocket reconnect (handled inside ``ManagedStreamSet``)
    forces a fresh per-symbol bootstrap, the same as Binance's own resync
    contract, instead of every routine subscription change doing so.
    """

    def __init__(
        self, adapter: BinanceFuturesAdapter, symbols: list[str], levels: int = 20
    ) -> None:
        self._adapter = adapter
        self._levels = levels
        self._symbols: list[str] = list(dict.fromkeys(s.upper() for s in symbols))
        self._books: dict[str, LocalOrderBook] = {}
        self._pending_bootstrap: set[str] = set(self._symbols)
        self._lock = asyncio.Lock()

        def to_stream(symbol: str) -> str:
            return f"{symbol.lower()}@depth@100ms"

        self._to_stream = to_stream
        streams = [to_stream(s) for s in self._symbols]
        self._stream_set = ManagedStreamSet(
            adapter, "orderbook", streams, to_stream=to_stream
        )

    async def update_symbols(self, symbols: list[str]) -> bool:
        normalized = list(dict.fromkeys(s.upper() for s in symbols))
        async with self._lock:
            if normalized == self._symbols:
                return False
            added = [s for s in normalized if s not in self._symbols]
            removed = [s for s in self._symbols if s not in normalized]
            for symbol in removed:
                self._books.pop(symbol, None)
                self._pending_bootstrap.discard(symbol)
            self._pending_bootstrap.update(added)
            self._symbols = normalized
        await self._stream_set.update_symbols(normalized)
        return True

    async def update_levels(self, levels: int) -> None:
        """Change local order-book depth truncation.

        This never touches the websocket: Binance always sends full depth
        diffs regardless of how many levels we keep locally, so a levels
        change that previously forced a full stream restart now just
        re-seeds each already-open book at the new depth without any
        connection loss or freshness gap.
        """
        async with self._lock:
            if levels == self._levels:
                return
            self._levels = levels
            symbols = list(self._books)
        for symbol in symbols:
            try:
                book = await self._bootstrap(symbol)
            except Exception as exc:
                logger.error(
                    "managed orderbook re-bootstrap for level change failed",
                    extra={"aitos_extra": {"symbol": symbol, "error": str(exc)}},
                )
                continue
            async with self._lock:
                self._books[symbol] = book

    async def _bootstrap(self, symbol: str) -> LocalOrderBook:
        book = LocalOrderBook(symbol=symbol, max_levels=self._levels)
        book.seed(
            await self._adapter.fetch_order_book(symbol, limit=max(self._levels, 50))
        )
        return book

    def __aiter__(self) -> "ManagedOrderBookStream":
        return self

    async def __anext__(self) -> OrderBookSnapshot:
        while True:
            data, stream_name = await self._stream_set.__anext__()
            symbol = stream_name.split("@", 1)[0].upper()
            async with self._lock:
                if symbol not in self._symbols:
                    continue
                needs_bootstrap = symbol in self._pending_bootstrap or (
                    symbol not in self._books
                )
            if needs_bootstrap:
                try:
                    book = await self._bootstrap(symbol)
                except Exception as exc:
                    logger.error(
                        "managed orderbook bootstrap failed",
                        extra={"aitos_extra": {"symbol": symbol, "error": str(exc)}},
                    )
                    continue
                async with self._lock:
                    self._books[symbol] = book
                    self._pending_bootstrap.discard(symbol)
                # Apply this same message below rather than discarding it --
                # LocalOrderBook.apply()'s awaiting-first-update bridge logic
                # is what reconciles the fresh REST snapshot with the live
                # diff stream, exactly as the original stream_order_book did.
            else:
                async with self._lock:
                    book = self._books[symbol]
            try:
                snapshot = book.apply(parse_depth_diff_ws(data))
            except OrderBookSequenceError:
                try:
                    book = await self._bootstrap(symbol)
                except Exception as exc:
                    logger.error(
                        "managed orderbook re-bootstrap failed",
                        extra={"aitos_extra": {"symbol": symbol, "error": str(exc)}},
                    )
                    continue
                async with self._lock:
                    self._books[symbol] = book
                continue
            if snapshot is not None:
                return snapshot

    async def aclose(self) -> None:
        await self._stream_set.aclose()
