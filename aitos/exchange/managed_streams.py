"""Managed Binance websocket stream sets with incremental SUBSCRIBE/UNSUBSCRIBE.

Avoids reconnect storms when the live universe changes: existing connections
stay open and only the delta is sent as control frames over the same websocket.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aitos.exchange.orderbook import LocalOrderBook, OrderBookSequenceError
from aitos.exchange.parsing import parse_depth_diff_ws
from aitos.logging_setup import get_logger
from aitos.models.market import OrderBookSnapshot

if TYPE_CHECKING:
    from aitos.exchange.binance import BinanceFuturesAdapter

logger = get_logger("aitos.exchange.managed_streams")

MANAGED_STREAM_QUEUE_SIZE = 20000


class ManagedStreamSet:
    """A single long-lived Binance combined-stream subscription that can be
    incrementally reconfigured without tearing down the websocket.

    Only a real websocket reconnect (handled inside ``ManagedStreamSet``)
    or the Binance-enforced lifecycle rotation forces a new connection.
    Symbol-set changes are applied with SUBSCRIBE / UNSUBSCRIBE control
    frames on the existing connection.
    """

    def __init__(
        self,
        adapter: BinanceFuturesAdapter,
        kind: str,
        streams: list[str],
        *,
        to_stream: Callable[[str], str] | None = None,
    ) -> None:
        self._adapter = adapter
        self._kind = kind
        self._to_stream = to_stream
        self._streams: list[str] = list(dict.fromkeys(streams))
        self._changed = asyncio.Event()
        self._queue: asyncio.Queue[tuple[Any, str]] = asyncio.Queue(
            maxsize=MANAGED_STREAM_QUEUE_SIZE
        )
        self._closed = False
        self._task = asyncio.create_task(
            self._run(), name=f"aitos-managed-stream-{kind}"
        )

    def update(self, streams: list[str]) -> None:
        """Replace the desired stream set; delta is applied without reconnect."""
        new = list(dict.fromkeys(streams))
        if new == self._streams:
            return
        self._streams = new
        self._changed.set()

    def current_streams(self) -> list[str]:
        return list(self._streams)

    async def update_symbols(self, symbols: list[str]) -> bool:
        if self._to_stream is None:
            raise RuntimeError(
                f"ManagedStreamSet({self._kind}) was not created with a "
                "symbol->stream converter; use update_streams() instead"
            )
        streams = [
            self._to_stream(s) for s in dict.fromkeys(s.upper() for s in symbols)
        ]
        await self.update_streams(streams)
        return True

    async def update_streams(self, streams: list[str]) -> bool:
        normalized = list(dict.fromkeys(streams))
        if normalized == self._streams:
            return False
        self._streams = normalized
        self._changed.set()
        return True

    async def _run(self) -> None:
        try:
            async for data, stream_name in self._adapter._connect_raw_dynamic(
                get_streams=lambda: self._streams,
                changed=self._changed,
                emit_reconnect=True,
                label=self._kind,
            ):
                if self._closed:
                    return
                try:
                    self._queue.put_nowait((data, stream_name))
                except asyncio.QueueFull:
                    while not self._queue.empty():
                        try:
                            self._queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        self._queue.put_nowait((data, stream_name))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "ManagedStreamSet producer failed",
                extra={"aitos_extra": {"kind": self._kind}},
            )

    def __aiter__(self) -> ManagedStreamSet:
        return self

    async def __anext__(self) -> tuple[Any, str]:
        if self._closed and self._queue.empty():
            raise StopAsyncIteration
        return await self._queue.get()

    async def aclose(self) -> None:
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None


class ManagedOrderBookStream:
    """Managed depth stream with per-symbol LocalOrderBook state that survives
    incremental symbol-set updates.

    Existing per-symbol ``LocalOrderBook`` state is preserved across
    symbol-set updates — only newly-added symbols get REST-bootstrapped,
    instead of every symbol being re-bootstrapped on every change.
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
        self._stream_set = ManagedStreamSet(adapter, "orderbook", streams)

    async def update_symbols(self, symbols: list[str]) -> bool:
        normalized = list(dict.fromkeys(s.upper() for s in symbols))
        async with self._lock:
            if normalized == self._symbols:
                return False
            added = [s for s in normalized if s not in self._symbols]
            removed = [s for s in self._symbols if s not in normalized]
            for symbol in removed:
                self._books.pop(symbol, None)
            self._pending_bootstrap.update(added)
            self._symbols = normalized
        if self._to_stream is not None:
            streams = [self._to_stream(s) for s in normalized]
            await self._stream_set.update_streams(streams)
        return True

    async def __aiter__(self) -> ManagedOrderBookStream:
        return self

    async def __anext__(self) -> OrderBookSnapshot:
        while True:
            data, stream_name = await self._stream_set.__anext__()
            if data is None:
                continue
            symbol = stream_name.split("@", 1)[0].upper()
            async with self._lock:
                book = self._books.get(symbol)
                if book is None:
                    if symbol not in self._symbols:
                        continue
                    try:
                        book = await self._bootstrap(symbol)
                    except Exception as exc:
                        logger.error(
                            "Managed orderbook bootstrap failed",
                            extra={
                                "aitos_extra": {
                                    "symbol": symbol,
                                    "error": str(exc),
                                }
                            },
                        )
                        continue
                    self._books[symbol] = book
                try:
                    snapshot = book.apply(parse_depth_diff_ws(data))
                except OrderBookSequenceError:
                    try:
                        book = await self._bootstrap(symbol)
                    except Exception as exc:
                        logger.error(
                            "Managed orderbook re-bootstrap failed",
                            extra={
                                "aitos_extra": {
                                    "symbol": symbol,
                                    "error": str(exc),
                                }
                            },
                        )
                        continue
                    self._books[symbol] = book
                    continue
                if snapshot is not None:
                    return snapshot

    async def _bootstrap(self, symbol: str) -> LocalOrderBook:
        book = LocalOrderBook(symbol=symbol, max_levels=self._levels)
        book.seed(
            await self._adapter.fetch_order_book(symbol, limit=max(self._levels, 50))
        )
        return book

    async def aclose(self) -> None:
        await self._stream_set.aclose()
