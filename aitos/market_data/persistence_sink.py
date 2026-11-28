"""Durable historical market-data persistence isolated from live socket ingestion."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from aitos.data.repository import MarketDataRepository
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.models.market import OrderBookSnapshot, TradeTick

from .bus import MarketDataBus
from .channels import GROUP_PERSISTENCE
from .contracts import MarketEvent, MarketEventType


class CanonicalMarketDataPersistenceSink:
    """Bounded durable writer whose Redis ACK follows the ClickHouse commit.

    Redis Streams decouple exchange socket ingestion from historical storage.
    The persistence consumer deliberately waits for the durable batch result
    before acknowledging its source entry, preserving replay on queue or store
    failures. Only configured event types and symbols enter this boundary.
    """

    def __init__(
        self,
        event_bus: EventBus,
        repository: MarketDataRepository | None,
        *,
        historical_book_symbols: tuple[str, ...] = ("BTCUSDT", "LTCUSDT"),
        historical_trade_symbols: tuple[str, ...] = ("BTCUSDT", "LTCUSDT"),
        book_interval_seconds: float = 1.0,
        historical_orderbook_levels: int = 100,
        queue_capacity: int = 10_000,
        workers: int = 4,
        batch_size: int = 100,
        batch_wait_seconds: float = 0.05,
        persist_event_types: tuple[MarketEventType, ...] = (
            MarketEventType.TRADE,
            MarketEventType.BOOK_DELTA,
            MarketEventType.BOOK_SNAPSHOT,
            MarketEventType.TICKER,
            MarketEventType.KLINE,
        ),
        market_types: tuple[str, ...] | None = None,
        canonical_only: bool = False,
        shutdown_flush_seconds: float = 10.0,
    ) -> None:
        self._bus = MarketDataBus(event_bus)
        self._repository = repository
        self._historical_books = {s.upper() for s in historical_book_symbols}
        self._historical_trades = {s.upper() for s in historical_trade_symbols}
        self._book_interval = max(0.1, book_interval_seconds)
        self._historical_orderbook_levels = max(
            1, min(1000, historical_orderbook_levels)
        )
        self._queue: asyncio.Queue[tuple[MarketEvent, asyncio.Future[None]]] = (
            asyncio.Queue(maxsize=queue_capacity)
        )
        self._queue_enqueued_at: dict[int, float] = {}
        self._queue_wait_samples_ms: deque[float] = deque(maxlen=2048)
        self._batch_write_samples_ms: deque[float] = deque(maxlen=2048)
        self._workers_count = max(1, workers)
        self._batch_size = max(1, batch_size)
        self._batch_wait = max(0.0, batch_wait_seconds)
        self._persist_event_types = set(persist_event_types)
        self._market_types = (
            tuple(item.lower() for item in market_types)
            if market_types is not None
            else None
        )
        self._canonical_only = canonical_only
        self._shutdown_flush_seconds = max(0.0, shutdown_flush_seconds)
        self._shutdown_dropped = 0
        self._subscriptions: list[Subscription] = []
        self._workers: list[asyncio.Task] = []
        self._last_book_persist: dict[str, datetime] = {}
        self._processed = 0
        self._errors = 0
        self._rejected = 0
        self._filtered = 0
        self._batches = 0
        self._versioned_events = 0
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized or self._repository is None:
            self._initialized = True
            return
        self._subscriptions = []
        for event_type in sorted(
            self._persist_event_types, key=lambda item: item.value
        ):
            market_types = self._market_types or (None,)
            for market_type in market_types:
                self._subscriptions.append(
                    await self._bus.subscribe(
                        event_type,
                        self._persist_before_ack,
                        group=GROUP_PERSISTENCE,
                        live_only=False,
                        market_type=market_type,
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
        if self._workers:
            try:
                await asyncio.wait_for(
                    self._queue.join(), timeout=self._shutdown_flush_seconds
                )
            except asyncio.TimeoutError:
                # A bounded shutdown is more important than hanging service teardown.
                pass
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                if isinstance(item, tuple) and len(item) == 2:
                    _, completion = item
                    self._queue_enqueued_at.pop(id(completion), None)
                    if not completion.done():
                        completion.cancel()
                self._shutdown_dropped += 1
                self._queue.task_done()
        self._initialized = False

    async def _wait(self, subscription: Subscription) -> None:
        try:
            await subscription._task
        except asyncio.CancelledError:
            pass

    async def _persist_before_ack(self, event: MarketEvent) -> None:
        """Return to Redis only after the event has reached durable storage."""
        completion = await self._enqueue(event)
        if completion is not None:
            await completion

    async def _enqueue(self, event: MarketEvent) -> asyncio.Future[None] | None:
        """Admit a historical event and expose its durable-write completion."""
        if self._repository is None:
            return
        if event.event_type not in self._persist_event_types:
            self._filtered += 1
            return
        if event.event_type is MarketEventType.TRADE:
            if event.symbol.upper() not in self._historical_trades:
                self._filtered += 1
                return
        elif event.event_type is MarketEventType.BOOK_DELTA:
            if event.symbol.upper() not in self._historical_books:
                self._filtered += 1
                return
        elif event.event_type in {MarketEventType.TICKER, MarketEventType.KLINE}:
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
        completion: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        try:
            self._queue.put_nowait((event, completion))
            self._queue_enqueued_at[id(completion)] = asyncio.get_running_loop().time()
        except asyncio.QueueFull:
            # Fail the Redis handler so the source entry is retried/DLQ'd, never ACKed.
            self._rejected += 1
            try:
                from aitos.forensics.data_flow_hooks import record_drop
                from aitos.forensics.data_flow_schema import (
                    DataIssueClass,
                    PipelineStage,
                )

                record_drop(
                    PipelineStage.PERSISTENCE.value,
                    symbol=event.symbol,
                    issue=DataIssueClass.DROPPED_BACKPRESSURE.value,
                )
            except Exception:
                pass
            raise RuntimeError("canonical market-data persistence queue is full")
        try:
            from aitos.forensics.data_flow_hooks import (
                note_trace,
                record_input,
                set_queue,
            )
            from aitos.forensics.data_flow_schema import PipelineStage

            record_input(
                PipelineStage.PERSISTENCE.value,
                symbol=event.symbol,
                source_age_ms=event.source_age_seconds * 1000.0,
            )
            note_trace(
                trace_id=event.trace_id,
                stage=PipelineStage.PERSISTENCE.value,
                symbol=event.symbol,
                event_id=event.event_id,
                extra={"phase": "queued"},
            )
            set_queue(
                PipelineStage.PERSISTENCE.value,
                depth=self._queue.qsize(),
                symbol=event.symbol,
            )
        except Exception:
            # Telemetry must never interfere with the durable persistence path.
            pass
        return completion

    async def _worker(self, worker_id: int) -> None:
        while True:
            event, completion = await self._queue.get()
            batch = [event]
            completions = [completion]
            try:
                deadline = asyncio.get_running_loop().time() + self._batch_wait
                while len(batch) < self._batch_size:
                    timeout = max(0.0, deadline - asyncio.get_running_loop().time())
                    if timeout == 0:
                        break
                    try:
                        next_event, next_completion = await asyncio.wait_for(
                            self._queue.get(), timeout
                        )
                        batch.append(next_event)
                        completions.append(next_completion)
                    except asyncio.TimeoutError:
                        break
                loop_now = asyncio.get_running_loop().time()
                for future in completions:
                    queued_at = self._queue_enqueued_at.pop(id(future), loop_now)
                    self._queue_wait_samples_ms.append(
                        max(0.0, loop_now - queued_at) * 1000.0
                    )
                write_started = time.perf_counter()
                try:
                    await self._persist_batch(batch)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._batch_write_samples_ms.append(
                        (time.perf_counter() - write_started) * 1000.0
                    )
                    self._errors += len(batch)
                    for future in completions:
                        if not future.done():
                            future.set_exception(exc)
                    try:
                        from aitos.forensics.data_flow_hooks import record_error
                        from aitos.forensics.data_flow_schema import PipelineStage

                        for event in batch:
                            record_error(
                                PipelineStage.PERSISTENCE.value,
                                symbol=event.symbol,
                                issue="processing_error",
                            )
                    except Exception:
                        pass
                else:
                    self._batch_write_samples_ms.append(
                        (time.perf_counter() - write_started) * 1000.0
                    )
                    self._processed += len(batch)
                    self._batches += 1
                    for future in completions:
                        if not future.done():
                            future.set_result(None)
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _persist_batch(self, events: list[MarketEvent]) -> None:
        if self._repository is None or not events:
            return
        grouped: dict[MarketEventType, list[MarketEvent]] = defaultdict(list)
        for event in events:
            grouped[event.event_type].append(event)

        versioned_batch_writer = getattr(
            self._repository, "save_market_events_versioned", None
        )
        versioned_writer = getattr(
            self._repository, "save_market_event_versioned", None
        )
        if versioned_batch_writer is not None:
            await versioned_batch_writer(events)
            self._versioned_events += len(events)
        elif versioned_writer is not None and len(events) == 1:
            # Compatibility is safe only for a singleton. Falling back to one
            # INSERT per event inside a multi-event batch recreates the
            # ClickHouse N+1 write amplification seen in staging.
            await versioned_writer(events[0])
            self._versioned_events += 1
        elif self._canonical_only or versioned_writer is not None:
            raise RuntimeError(
                "versioned market-event persistence requires the bulk "
                "save_market_events_versioned(events) writer for batches"
            )

        if self._canonical_only:
            trades = []
            deltas = []
            books = []
        else:
            trades = grouped.get(MarketEventType.TRADE, [])
        if trades:
            await self._repository.save_trade_ticks(
                [TradeTick.from_dict(dict(event.payload)) for event in trades]
            )

        if not self._canonical_only:
            deltas = grouped.get(MarketEventType.BOOK_DELTA, [])
        if deltas:
            await self._repository.save_depth_events(
                [
                    {
                        "symbol": event.symbol,
                        "event_time": event.event_time,
                        "payload": dict(event.payload),
                    }
                    for event in deltas
                ]
            )

        if not self._canonical_only:
            books = grouped.get(MarketEventType.BOOK_SNAPSHOT, [])
        if books:
            snapshots: list[OrderBookSnapshot] = []
            for event in books:
                payload: dict[str, Any] = dict(event.payload)
                payload["symbol"] = event.symbol
                payload["timestamp"] = event.event_time.isoformat()
                snapshot = OrderBookSnapshot.from_dict(payload)
                snapshots.append(
                    replace(
                        snapshot,
                        bids=snapshot.bids[: self._historical_orderbook_levels],
                        asks=snapshot.asks[: self._historical_orderbook_levels],
                    )
                )
            await self._repository.save_order_book_snapshots(snapshots)

        try:
            from aitos.forensics.data_flow_hooks import (
                note_trace,
                record_output,
                set_queue,
            )
            from aitos.forensics.data_flow_schema import PipelineStage

            for event in events:
                latency_ms = max(
                    0.0,
                    (datetime.now(timezone.utc) - event.ingest_time).total_seconds()
                    * 1000.0,
                )
                record_output(
                    PipelineStage.PERSISTENCE.value,
                    symbol=event.symbol,
                    latency_ms=latency_ms,
                )
                note_trace(
                    trace_id=event.trace_id,
                    stage=PipelineStage.PERSISTENCE.value,
                    symbol=event.symbol,
                    event_id=event.event_id,
                    latency_ms=latency_ms,
                    extra={"phase": "persisted", "event_type": event.event_type.value},
                )
                set_queue(
                    PipelineStage.PERSISTENCE.value,
                    depth=self._queue.qsize(),
                    symbol=event.symbol,
                )
        except Exception:
            # Observability must not turn successful persistence into a failure.
            pass

    @staticmethod
    def _latency_summary(samples: deque[float]) -> dict[str, float | int | None]:
        if not samples:
            return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
        ordered = sorted(samples)

        def percentile(fraction: float) -> float:
            index = max(
                0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5))
            )
            return round(ordered[index], 3)

        return {
            "count": len(ordered),
            "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95),
            "max_ms": round(ordered[-1], 3),
        }

    def snapshot(self) -> dict[str, object]:
        return {
            "initialized": self._initialized,
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "queue_wait": self._latency_summary(self._queue_wait_samples_ms),
            "batch_write": self._latency_summary(self._batch_write_samples_ms),
            "processed": self._processed,
            "errors": self._errors,
            "rejected": self._rejected,
            "filtered": self._filtered,
            "workers": len(self._workers),
            "batch_size": self._batch_size,
            "batch_wait_seconds": self._batch_wait,
            "batches": self._batches,
            "versioned_events": self._versioned_events,
            "persist_event_types": sorted(
                item.value for item in self._persist_event_types
            ),
            "market_types": list(self._market_types or ()),
            "canonical_only": self._canonical_only,
            "shutdown_flush_seconds": self._shutdown_flush_seconds,
            "shutdown_dropped": self._shutdown_dropped,
            "historical_book_symbols": sorted(self._historical_books),
            "historical_orderbook_levels": self._historical_orderbook_levels,
            "historical_trade_symbols": sorted(self._historical_trades),
            "backpressure_policy": "drop_history_never_block_live",
        }
