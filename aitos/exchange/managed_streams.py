"""Managed Binance websocket stream sets with incremental SUBSCRIBE/UNSUBSCRIBE.

Avoids reconnect storms when the live universe changes: existing connections
stay open and only the delta is sent as control frames over the same websocket.
"""

from __future__ import annotations

import asyncio
import time
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
        self._queue: asyncio.Queue[tuple[Any, str, float]] = asyncio.Queue(
            maxsize=MANAGED_STREAM_QUEUE_SIZE
        )
        self._queue_stats: dict[str, Any] = {
            "enqueued": 0,
            "dequeued": 0,
            "overflow_events": 0,
            "dropped_events": 0,
            "max_depth": 0,
            "queue_wait_total_ms": 0.0,
            "queue_wait_max_ms": 0.0,
            "last_overflow_at": None,
        }
        self._closed = False
        self._publish_queue_stats()
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

    def _publish_queue_stats(self) -> None:
        """Publish bounded per-stream queue telemetry into transport health."""
        transport = getattr(self._adapter, "_ws_transport", None)
        if not isinstance(transport, dict):
            return
        queues = transport.setdefault("managed_queues", {})
        if not isinstance(queues, dict):
            return
        stats = dict(self._queue_stats)
        stats["depth"] = self._queue.qsize()
        stats["capacity"] = self._queue.maxsize
        count = int(stats.get("dequeued", 0))
        stats["queue_wait_avg_ms"] = round(
            float(stats.get("queue_wait_total_ms", 0.0)) / count, 3
        ) if count else 0.0
        stats["queue_wait_total_ms"] = round(
            float(stats.get("queue_wait_total_ms", 0.0)), 3
        )
        stats["queue_wait_max_ms"] = round(
            float(stats.get("queue_wait_max_ms", 0.0)), 3
        )
        queues[self._kind] = stats

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
                # Skip None sentinel from reconnects
                if data is None:
                    continue
                queued_at = time.monotonic()
                try:
                    self._queue.put_nowait((data, stream_name, queued_at))
                    self._queue_stats["enqueued"] += 1
                    self._queue_stats["max_depth"] = max(
                        int(self._queue_stats["max_depth"]), self._queue.qsize()
                    )
                    self._publish_queue_stats()
                except asyncio.QueueFull:
                    # Keep the newest event, but account for every discarded
                    # event. Order-book sequence gaps are detected downstream
                    # by LocalOrderBook and trigger snapshot recovery.
                    self._queue_stats["overflow_events"] += 1
                    self._queue_stats["last_overflow_at"] = time.time()
                    while not self._queue.empty():
                        try:
                            self._queue.get_nowait()
                            self._queue.task_done()
                            self._queue_stats["dropped_events"] += 1
                        except asyncio.QueueEmpty:
                            break
                    # Retry once after the explicit freshness-first drain.
                    try:
                        self._queue.put_nowait((data, stream_name, queued_at))
                        self._queue_stats["enqueued"] += 1
                        self._queue_stats["max_depth"] = max(
                            int(self._queue_stats["max_depth"]), self._queue.qsize()
                        )
                    except asyncio.QueueFull:
                        self._queue_stats["dropped_events"] += 1
                        logger.warning(
                            "ManagedStreamSet queue full after drain, dropping event",
                            extra={
                                "aitos_extra": {
                                    "kind": self._kind,
                                    "overflow_events": self._queue_stats[
                                        "overflow_events"
                                    ],
                                    "dropped_events": self._queue_stats[
                                        "dropped_events"
                                    ],
                                }
                            },
                        )
                    self._publish_queue_stats()
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
        data, stream_name, queued_at = await self._queue.get()
        waited_ms = max(0.0, (time.monotonic() - queued_at) * 1000.0)
        self._queue_stats["dequeued"] += 1
        self._queue_stats["queue_wait_total_ms"] += waited_ms
        self._queue_stats["queue_wait_max_ms"] = max(
            float(self._queue_stats["queue_wait_max_ms"]), waited_ms
        )
        self._publish_queue_stats()
        return data, stream_name

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
                    diff = parse_depth_diff_ws(data)
                    book = LocalOrderBook(symbol=symbol, max_levels=self._levels)
                    book.seed_from_diff(diff)
                    self._books[symbol] = book
                    return book.snapshot(diff.event_time_ms)
            try:
                snapshot = book.apply(parse_depth_diff_ws(data))
            except OrderBookSequenceError:
                diff = parse_depth_diff_ws(data)
                book = LocalOrderBook(symbol=symbol, max_levels=self._levels)
                book.seed_from_diff(diff)
                async with self._lock:
                    self._books[symbol] = book
                return book.snapshot(diff.event_time_ms)
            if snapshot is not None:
                return snapshot

    async def aclose(self) -> None:
        await self._stream_set.aclose()
