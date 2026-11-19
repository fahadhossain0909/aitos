"""Opt-in, bounded, read-only market-data projection for cross-market research.

This consumer is intentionally lossy under overload and has no execution,
capital, or risk-policy authority. The canonical MarketDataBus remains the
source of truth; durable sinks use their own independent consumer groups.
"""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from aitos.market_data.bus import MarketDataBus
from aitos.market_data.contracts import MarketEvent as CanonicalMarketEvent
from aitos.market_data.contracts import MarketEventType

from .bridges import market_data_event_to_domain
from .contracts import Instrument
from .engine import CrossMarketIntelligenceEngine

InstrumentResolver = Callable[[CanonicalMarketEvent], Instrument | None]


@dataclass(frozen=True, slots=True)
class ShadowConsumerSnapshot:
    started: bool
    queued: int
    accepted: int
    processed: int
    rejected_stale: int
    rejected_unresolved: int
    rejected_out_of_order: int
    rejected_overload: int
    rejected_invalid_price: int
    duplicate_events: int
    processing_errors: int


class MarketDataShadowConsumer:
    """Bounded MarketDataBus subscriber feeding only cross-market features.

    The caller must explicitly construct and start this consumer. It subscribes
    to an allow-listed set of ticker market types using live-only consumer
    groups. When its bounded queue is full, the newest event is dropped and
    counted rather than blocking the market-data publisher or growing memory.
    """

    def __init__(
        self,
        *,
        engine: CrossMarketIntelligenceEngine,
        instrument_resolver: InstrumentResolver,
        market_types: tuple[str, ...],
        consumer_group: str = "cross-market-shadow-v1",
        queue_capacity: int = 2048,
        max_event_age_seconds: float = 5.0,
        max_instruments: int = 500,
        dedupe_capacity: int = 8192,
    ) -> None:
        normalized = tuple(
            dict.fromkeys(str(value).strip().lower() for value in market_types)
        )
        if not normalized or any(not value for value in normalized):
            raise ValueError("market_types must contain explicit non-empty values")
        if queue_capacity < 1 or max_instruments < 1 or dedupe_capacity < 1:
            raise ValueError("queue and capacity limits must be positive")
        if max_event_age_seconds <= 0:
            raise ValueError("max_event_age_seconds must be positive")
        if not consumer_group.strip():
            raise ValueError("consumer_group must be non-empty")

        self._engine = engine
        self._resolver = instrument_resolver
        self._market_types = normalized
        self._group = consumer_group.strip()
        self._max_age = max_event_age_seconds
        self._max_instruments = max_instruments
        self._dedupe_capacity = dedupe_capacity
        self._queue: asyncio.Queue[CanonicalMarketEvent] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._subscriptions = []
        self._worker: asyncio.Task[None] | None = None
        self._started = False
        self._instrument_ids: set[str] = set()
        self._latest_event_time: dict[str, datetime] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._counts = {
            "accepted": 0,
            "processed": 0,
            "rejected_stale": 0,
            "rejected_unresolved": 0,
            "rejected_out_of_order": 0,
            "rejected_overload": 0,
            "rejected_invalid_price": 0,
            "duplicate_events": 0,
            "processing_errors": 0,
        }

    @staticmethod
    def _event_price(event: CanonicalMarketEvent) -> float | None:
        payload = event.payload
        try:
            bid = payload.get("bid_price", payload.get("bidPrice"))
            ask = payload.get("ask_price", payload.get("askPrice"))
            if bid is not None and ask is not None:
                value = (float(bid) + float(ask)) / 2.0
            else:
                raw = (
                    payload.get("last_price")
                    or payload.get("lastPrice")
                    or payload.get("price")
                    or payload.get("close")
                    or payload.get("c")
                )
                value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    async def start(self, market_bus: MarketDataBus) -> None:
        """Start explicit live-only shadow subscriptions; safe to call once."""
        if self._started:
            return
        self._worker = asyncio.create_task(
            self._run_worker(), name="aitos-cross-market-shadow"
        )
        try:
            for market_type in self._market_types:
                self._subscriptions.append(
                    await market_bus.subscribe(
                        MarketEventType.TICKER,
                        self._on_event,
                        group=f"{self._group}-{market_type}",
                        live_only=True,
                        market_type=market_type,
                    )
                )
        except Exception:
            await self.stop(drain=False)
            raise
        self._started = True

    async def _on_event(self, event: CanonicalMarketEvent) -> None:
        if event.event_type is not MarketEventType.TICKER:
            return
        if event.event_time.tzinfo is None:
            self._counts["rejected_stale"] += 1
            return
        age = (datetime.now(timezone.utc) - event.event_time).total_seconds()
        if age < -1.0 or age > self._max_age:
            self._counts["rejected_stale"] += 1
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._counts["rejected_overload"] += 1
            return
        self._counts["accepted"] += 1

    async def _run_worker(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                self._process(event)
            except Exception:
                # Shadow analytics must not propagate into market-data or trading.
                self._counts["processing_errors"] += 1
            finally:
                self._queue.task_done()

    def _process(self, event: CanonicalMarketEvent) -> None:
        market_type = str(event.market_type or event.market).strip().lower()
        if market_type not in self._market_types:
            self._counts["rejected_unresolved"] += 1
            return
        if event.event_id in self._seen:
            self._counts["duplicate_events"] += 1
            return

        instrument = self._resolver(event)
        if instrument is None:
            self._counts["rejected_unresolved"] += 1
            return
        is_new_instrument = instrument.id not in self._instrument_ids
        if is_new_instrument and len(self._instrument_ids) >= self._max_instruments:
            self._counts["rejected_unresolved"] += 1
            return

        previous = self._latest_event_time.get(instrument.id)
        if previous is not None and event.event_time <= previous:
            self._counts["rejected_out_of_order"] += 1
            return

        price = self._event_price(event)
        if price is None:
            self._counts["rejected_invalid_price"] += 1
            return

        try:
            domain_event = market_data_event_to_domain(event, instrument)
        except (TypeError, ValueError):
            self._counts["rejected_unresolved"] += 1
            return
        self._engine.ingest_price(domain_event, price)
        if is_new_instrument:
            self._instrument_ids.add(instrument.id)
        self._latest_event_time[instrument.id] = event.event_time
        self._seen[event.event_id] = None
        self._seen.move_to_end(event.event_id)
        while len(self._seen) > self._dedupe_capacity:
            self._seen.popitem(last=False)
        self._counts["processed"] += 1

    async def stop(self, *, drain: bool = True) -> None:
        """Cancel subscriptions and stop the worker; optionally drain queued work."""
        subscriptions, self._subscriptions = self._subscriptions, []
        for subscription in subscriptions:
            subscription.cancel()
        worker, self._worker = self._worker, None
        if worker is not None:
            if drain:
                await self._queue.join()
            else:
                while not self._queue.empty():
                    self._queue.get_nowait()
                    self._queue.task_done()
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._started = False

    def snapshot(self) -> ShadowConsumerSnapshot:
        return ShadowConsumerSnapshot(
            started=self._started,
            queued=self._queue.qsize(),
            **self._counts,
        )
