"""Durable ClickHouse sink for canonical market-data events.

Redis remains the bounded transport. This sink is the only market-data history
writer used by the V1 path; it keeps a bounded work queue and never treats REST
recovery as live state.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from aitos.data.repository import MarketDataRepository
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.models.market import Kline, OrderBookSnapshot, TradeTick

from .bus import MarketDataBus
from .channels import GROUP_PERSISTENCE
from .contracts import MarketEvent, MarketEventType


class CanonicalMarketDataPersistenceSink:
    """Bounded asynchronous ClickHouse writer for canonical events."""

    def __init__(
        self,
        event_bus: EventBus,
        repository: MarketDataRepository | None,
        *,
        historical_book_symbols: tuple[str, ...] = ("BTCUSDT", "LTCUSDT"),
        book_interval_seconds: float = 1.0,
        queue_capacity: int = 10_000,
        workers: int = 4,
    ) -> None:
        self._bus = MarketDataBus(event_bus)
        self._repository = repository
        self._historical_books = {s.upper() for s in historical_book_symbols}
        self._book_interval = max(0.1, book_interval_seconds)
        self._queue: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=queue_capacity)
        self._workers_count = max(1, workers)
        self._subscriptions: list[Subscription] = []
        self._workers: list[asyncio.Task] = []
        self._last_book_persist: dict[str, datetime] = {}
        self._processed = 0
        self._errors = 0
        self._rejected = 0
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized or self._repository is None:
            self._initialized = True
            return
        self._subscriptions = [
            await self._bus.subscribe(
                MarketEventType.TRADE,
                self._enqueue,
                group=GROUP_PERSISTENCE,
                live_only=True,
            ),
            await self._bus.subscribe(
                MarketEventType.BOOK_SNAPSHOT,
                self._enqueue,
                group=GROUP_PERSISTENCE,
                live_only=True,
            ),
            await self._bus.subscribe(
                MarketEventType.KLINE,
                self._enqueue,
                group=GROUP_PERSISTENCE,
                live_only=True,
            ),
        ]
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
        if self._repository is None:
            return
        if event.event_type is MarketEventType.BOOK_SNAPSHOT:
            if event.symbol.upper() not in self._historical_books:
                return
            now = datetime.now(timezone.utc)
            previous = self._last_book_persist.get(event.symbol)
            if (
                previous is not None
                and (now - previous).total_seconds() < self._book_interval
            ):
                return
            self._last_book_persist[event.symbol] = now
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Do not silently discard high-value events: leaving the Redis
            # delivery unacked lets the consumer group retain the event.
            self._rejected += 1
            raise

    async def _worker(self, worker_id: int) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._persist(event)
                self._processed += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self._errors += 1
                raise
            finally:
                self._queue.task_done()

    async def _persist(self, event: MarketEvent) -> None:
        if self._repository is None:
            return
        payload: dict[str, Any] = dict(event.payload)
        if event.event_type is MarketEventType.TRADE:
            await self._repository.save_trade_tick(TradeTick.from_dict(payload))
        elif event.event_type is MarketEventType.BOOK_SNAPSHOT:
            payload["symbol"] = event.symbol
            payload["timestamp"] = event.event_time.isoformat()
            await self._repository.save_order_book_snapshot(
                OrderBookSnapshot.from_dict(payload)
            )
        elif event.event_type is MarketEventType.KLINE:
            payload["symbol"] = event.symbol
            await self._repository.save_kline(Kline.from_dict(payload))

    def snapshot(self) -> dict[str, object]:
        return {
            "initialized": self._initialized,
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "processed": self._processed,
            "errors": self._errors,
            "rejected": self._rejected,
            "workers": len(self._workers),
            "historical_book_symbols": sorted(self._historical_books),
        }
