"""Best-effort historical market-data persistence isolated from live ingestion."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from aitos.data.repository import MarketDataRepository
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.models.market import OrderBookSnapshot, TradeTick

from .bus import MarketDataBus
from .channels import GROUP_PERSISTENCE
from .contracts import MarketEvent, MarketEventType


class CanonicalMarketDataPersistenceSink:
    """Bounded historical writer; never makes live ingestion wait for ClickHouse.

    The canonical Redis bus carries the full live universe. This sink is a
    deliberately narrow historical boundary: only configured event types and
    configured historical symbols are admitted. Low-value live event classes
    must not silently consume the historical persistence queue.

    Historical persistence intentionally uses a single worker by default.
    ClickHouse inserts are relatively slow on the constrained paper-trading VM;
    several concurrent workers turn a small historical stream into many tiny,
    overlapping inserts and increase storage/Redis/event-loop contention. The
    queue remains bounded and best-effort, so this does not backpressure live
    market-data ingestion.
    """

    def __init__(
        self,
        event_bus: EventBus,
        repository: MarketDataRepository | None,
        *,
        historical_book_symbols: tuple[str, ...] = ("BTCUSDT", "LTCUSDT"),
        historical_trade_symbols: tuple[str, ...] = ("BTCUSDT", "LTCUSDT"),
        book_interval_seconds: float = 1.0,
        queue_capacity: int = 10_000,
        workers: int = 1,
        batch_size: int = 100,
        batch_wait_seconds: float = 0.05,
        persist_event_types: tuple[MarketEventType, ...] = (
            MarketEventType.TRADE,
            MarketEventType.BOOK_SNAPSHOT,
        ),
    ) -> None:
        self._bus = MarketDataBus(event_bus)
        self._repository = repository
        self._historical_books = {s.upper() for s in historical_book_symbols}
        self._historical_trades = {s.upper() for s in historical_trade_symbols}
        self._book_interval = max(0.1, book_interval_seconds)
        self._queue: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=queue_capacity)
        self._workers_count = max(1, workers)
        self._batch_size = max(1, batch_size)
        self._batch_wait = max(0.0, batch_wait_seconds)
        self._persist_event_types = set(persist_event_types)
        self._subscriptions: list[Subscription] = []
        self._workers: list[asyncio.Task] = []
        self._last_book_persist: dict[str, datetime] = {}
        self._processed = 0
        self._errors = 0
        self._rejected = 0
        self._filtered = 0
        self._batches = 0
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized or self._repository is None:
            self._initialized = True
            return
        self._subscriptions = []
        for event_type in sorted(
            self._persist_event_types, key=lambda item: item.value
        ):
            self._subscriptions.append(
                await self._bus.subscribe(
                    event_type,
                    self._enqueue,
                    group=GROUP_PERSISTENCE,
                    live_only=True,
                )
            )
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"market-data-persistence-{i}")
            for i in range(self._workers_count)
        ]
        self._initialized = True

    async def shutdown(self) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        if self._subscriptions:
            await asyncio.gather(
                *(self._wait(s) for s in self._subscriptions), return_exceptions=True
            )
        self._subscriptions.clear()
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._initialized = False

    async def _wait(self, subscription: Subscription) -> None:
        try:
            await subscription._task
        except asyncio.CancelledError:
            pass

    async def _enqueue(self, event: MarketEvent) -> None:
        """Apply the historical boundary before consuming queue capacity."""
        if self._repository is None:
            return
        if event.event_type not in self._persist_event_types:
            self._filtered += 1
            return
        if event.event_type is MarketEventType.TRADE:
            if event.symbol.upper() not in self._historical_trades:
                self._filtered += 1
                return
        elif event.event_type is MarketEventType.BOOK_SNAPSHOT:
            if event.symbol.upper() not in self._historical_books:
                self._filtered += 1
                return
            now = datetime.now(timezone.utc)
            previous = self._last_book_persist.get(event.symbol)
            if (
                previous is not None
                and (now - previous).total_seconds() < self._book_interval
            ):
                self._filtered += 1
                return
            self._last_book_persist[event.symbol] = now
        else:
            self._filtered += 1
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Historical persistence is best-effort and must never block live ingestion.
            self._rejected += 1

    async def _worker(self, worker_id: int) -> None:
        while True:
            event = await self._queue.get()
            batch = [event]
            try:
                deadline = asyncio.get_running_loop().time() + self._batch_wait
                while len(batch) < self._batch_size:
                    timeout = max(0.0, deadline - asyncio.get_running_loop().time())
                    if timeout == 0:
                        break
                    try:
                        batch.append(await asyncio.wait_for(self._queue.get(), timeout))
                    except asyncio.TimeoutError:
                        break
                try:
                    await self._persist_batch(batch)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._errors += len(batch)
                else:
                    self._processed += len(batch)
                    self._batches += 1
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _persist_batch(self, events: list[MarketEvent]) -> None:
        if self._repository is None or not events:
            return
        grouped: dict[MarketEventType, list[MarketEvent]] = defaultdict(list)
        for event in events:
            grouped[event.event_type].append(event)

        trades = grouped.get(MarketEventType.TRADE, [])
        if trades:
            await self._repository.save_trade_ticks(
                [TradeTick.from_dict(dict(event.payload)) for event in trades]
            )

        books = grouped.get(MarketEventType.BOOK_SNAPSHOT, [])
        if books:
            snapshots: list[OrderBookSnapshot] = []
            for event in books:
                payload: dict[str, Any] = dict(event.payload)
                payload["symbol"] = event.symbol
                payload["timestamp"] = event.event_time.isoformat()
                snapshots.append(OrderBookSnapshot.from_dict(payload))
            await self._repository.save_order_book_snapshots(snapshots)

    def snapshot(self) -> dict[str, object]:
        return {
            "initialized": self._initialized,
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "processed": self._processed,
            "errors": self._errors,
            "rejected": self._rejected,
            "filtered": self._filtered,
            "workers": len(self._workers),
            "batch_size": self._batch_size,
            "batch_wait_seconds": self._batch_wait,
            "batches": self._batches,
            "persist_event_types": sorted(
                item.value for item in self._persist_event_types
            ),
            "historical_book_symbols": sorted(self._historical_books),
            "historical_trade_symbols": sorted(self._historical_trades),
            "backpressure_policy": "drop_history_never_block_live",
        }
