"""Resilient raw-depth collector for the BTC/LTC research tier."""

from __future__ import annotations

import asyncio

from .contracts import MarketEvent
from .deep_orderbook import DEEP_SYMBOLS, DeepOrderBookStore


class DeepOrderBookCollector:
    """Collect every deep delta into a bounded durable writer queue."""

    def __init__(
        self,
        adapter: object,
        store: DeepOrderBookStore,
        *,
        symbols: tuple[str, ...] = tuple(sorted(DEEP_SYMBOLS)),
        queue_capacity: int = 20_000,
        retry_delay_seconds: float = 0.5,
    ) -> None:
        self._adapter = adapter
        self._store = store
        self._symbols = tuple(s.upper() for s in symbols if s.upper() in DEEP_SYMBOLS)
        self._queue: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=queue_capacity)
        self._retry_delay = max(0.1, retry_delay_seconds)
        self._producer: asyncio.Task | None = None
        self._writer: asyncio.Task | None = None
        self._running = False
        self._received = 0
        self._queued = 0
        self._persist_errors = 0

    async def start(self) -> None:
        if self._running or not self._symbols:
            return
        await self._store.initialize()
        self._running = True
        self._producer = asyncio.create_task(
            self._produce(), name="deep-orderbook-producer"
        )
        self._writer = asyncio.create_task(self._write(), name="deep-orderbook-writer")

    async def stop(self) -> None:
        self._running = False
        for task in (self._producer, self._writer):
            if task is not None:
                task.cancel()
        tasks = [task for task in (self._producer, self._writer) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._producer = None
        self._writer = None

    async def _produce(self) -> None:
        stream = self._adapter.stream_order_book_deltas
        while self._running:
            try:
                async for event in stream(list(self._symbols)):
                    if not self._running:
                        return
                    self._received += 1
                    await self._queue.put(event)
                    self._queued += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(self._retry_delay)

    async def _write(self) -> None:
        while self._running:
            event = await self._queue.get()
            try:
                while self._running:
                    try:
                        await self._store.persist(event)
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self._persist_errors += 1
                        await asyncio.sleep(self._retry_delay)
            finally:
                self._queue.task_done()

    def snapshot(self) -> dict[str, int | bool | list[str]]:
        deep = self._store.snapshot()
        return {
            "running": self._running,
            "symbols": list(self._symbols),
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "received": self._received,
            "queued": self._queued,
            "persist_errors": self._persist_errors,
            **deep,
        }
