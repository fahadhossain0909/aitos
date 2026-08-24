"""Durable persistence for Market OS live-state and order-flow events."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.data.repository import MarketDataRepository
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.logging_setup import get_logger

logger = get_logger("aitos.data.market_os_persistence")

BATCH_SIZE = 500
BATCH_FLUSH_INTERVAL_SECONDS = 1.0

CREATE_MARKET_ORDERFLOW = """
CREATE TABLE IF NOT EXISTS market_orderflow (
    time DateTime64(3, 'UTC'), event_id String, symbol LowCardinality(String),
    trade_count UInt64, buy_volume Float64, sell_volume Float64, delta Float64,
    cvd Float64, buy_ratio Float64, aggression Float64, imbalance Float64,
    vwap Float64, last_price Float64, direction String
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time)
ORDER BY (symbol, time, event_id)
"""

CREATE_MARKET_LIQUIDITY = """
CREATE TABLE IF NOT EXISTS market_liquidity_events (
    time DateTime64(3, 'UTC'), event_id String, symbol LowCardinality(String),
    kind LowCardinality(String), side LowCardinality(String), score Float64,
    price Float64, details_json String, last_update_id UInt64
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time)
ORDER BY (symbol, time, event_id)
"""

CREATE_MARKET_LIVE_STATE = """
CREATE TABLE IF NOT EXISTS market_live_state (
    time DateTime64(3, 'UTC'), event_id String, symbol LowCardinality(String),
    trade_count UInt64, order_flow_json String, liquidity_events_json String,
    best_bid Nullable(Float64), best_ask Nullable(Float64),
    state_timestamp Nullable(DateTime64(3, 'UTC'))
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time)
ORDER BY (symbol, time, event_id)
"""


class MarketOSPersistence(AITOSModule):
    """Persist Market OS events using bounded ClickHouse batches."""

    def __init__(
        self,
        event_bus: EventBus,
        repository: Optional[MarketDataRepository],
        batch_size: int = BATCH_SIZE,
        flush_interval_seconds: float = BATCH_FLUSH_INTERVAL_SECONDS,
    ) -> None:
        self._event_bus = event_bus
        self._repository = repository
        self._subscriptions: list[Subscription] = []
        self._initialized = False
        self._events_persisted = 0
        self._errors = 0
        self._last_event_time: Optional[str] = None
        self._batch_size = max(1, batch_size)
        self._flush_interval_seconds = max(0.05, flush_interval_seconds)
        self._buffers: dict[str, list[list[Any]]] = {
            "market_orderflow": [],
            "market_liquidity_events": [],
            "market_live_state": [],
        }
        self._column_names = {
            "market_orderflow": [
                "time", "event_id", "symbol", "trade_count", "buy_volume",
                "sell_volume", "delta", "cvd", "buy_ratio", "aggression",
                "imbalance", "vwap", "last_price", "direction",
            ],
            "market_liquidity_events": [
                "time", "event_id", "symbol", "kind", "side", "score",
                "price", "details_json", "last_update_id",
            ],
            "market_live_state": [
                "time", "event_id", "symbol", "trade_count", "order_flow_json",
                "liquidity_events_json", "best_bid", "best_ask", "state_timestamp",
            ],
        }
        self._flush_lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task[None]] = None

    @property
    def module_id(self) -> str:
        return "market-os-persistence"

    @property
    def version(self) -> str:
        return "1.1.0"

    async def initialize(self, config: Dict[str, Any]) -> None:
        if self._initialized:
            return
        if self._repository is None:
            logger.warning("Market OS persistence disabled: ClickHouse repository unavailable")
            self._initialized = True
            return
        await self._repository._client.command(CREATE_MARKET_ORDERFLOW)
        await self._repository._client.command(CREATE_MARKET_LIQUIDITY)
        await self._repository._client.command(CREATE_MARKET_LIVE_STATE)
        self._subscriptions = [
            await self._event_bus.subscribe("market.orderflow.*", self._handle_orderflow, group="market-os-orderflow"),
            await self._event_bus.subscribe("market.liquidity.*", self._handle_liquidity, group="market-os-liquidity"),
            await self._event_bus.subscribe("market.live_state.*", self._handle_live_state, group="market-os-live-state"),
        ]
        self._flush_task = asyncio.create_task(self._flush_loop())
        self._initialized = True
        logger.info(
            "Market OS persistence initialized (batch_size=%s, flush_interval=%.2fs)",
            self._batch_size,
            self._flush_interval_seconds,
        )

    async def health_check(self) -> HealthStatus:
        status = ModuleStatus.HEALTHY if self._repository is not None else ModuleStatus.DEGRADED
        return HealthStatus(
            module_id=self.module_id,
            status=status,
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={
                "events_persisted": self._events_persisted,
                "errors": self._errors,
                "pending_batches": sum(len(rows) for rows in self._buffers.values()),
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions.clear()
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self._flush_all()

    async def emit_events(self):
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> Optional[EventResponse]:
        return None

    async def _handle_orderflow(self, event: Event) -> Optional[EventResponse]:
        payload = event.payload
        symbol = _symbol_from_topic(event.topic)
        row = [
            event.created_at, event.event_id, symbol,
            int(payload.get("trade_count", 0)), float(payload.get("buy_volume", 0.0)),
            float(payload.get("sell_volume", 0.0)), float(payload.get("delta", 0.0)),
            float(payload.get("cvd", 0.0)), float(payload.get("buy_ratio", 0.0)),
            float(payload.get("aggression", 0.0)), float(payload.get("imbalance", 0.0)),
            float(payload.get("vwap", 0.0)), float(payload.get("last_price", 0.0)),
            str(payload.get("direction", "")),
        ]
        await self._enqueue("market_orderflow", row, event)
        return None

    async def _handle_liquidity(self, event: Event) -> Optional[EventResponse]:
        payload = event.payload
        symbol = _symbol_from_topic(event.topic)
        row = [
            event.created_at, event.event_id, symbol, str(payload.get("kind", "")),
            str(payload.get("side", "")), float(payload.get("score", 0.0)),
            float(payload.get("price", 0.0)),
            json.dumps(payload.get("details", {}), sort_keys=True, default=str),
            int(payload.get("last_update_id", 0)),
        ]
        await self._enqueue("market_liquidity_events", row, event)
        return None

    async def _handle_live_state(self, event: Event) -> Optional[EventResponse]:
        payload = event.payload
        symbol = _symbol_from_topic(event.topic)
        row = [
            event.created_at, event.event_id, symbol, int(payload.get("trade_count", 0)),
            json.dumps(payload.get("order_flow"), sort_keys=True, default=str),
            json.dumps(payload.get("liquidity_events", []), sort_keys=True, default=str),
            _optional_float(payload.get("best_bid")), _optional_float(payload.get("best_ask")),
            _parse_timestamp(payload.get("timestamp")),
        ]
        await self._enqueue("market_live_state", row, event)
        return None

    async def _enqueue(self, table: str, row: list[Any], event: Event) -> None:
        if self._repository is None:
            return
        flush_now = False
        async with self._flush_lock:
            self._buffers[table].append(row)
            flush_now = len(self._buffers[table]) >= self._batch_size
        if flush_now:
            await self._flush_table(table, event)
        else:
            self._record_success(event)

    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._flush_interval_seconds)
                await self._flush_all()
        except asyncio.CancelledError:
            raise

    async def _flush_all(self) -> None:
        for table in tuple(self._buffers):
            await self._flush_table(table, None)

    async def _flush_table(self, table: str, event: Optional[Event]) -> None:
        if self._repository is None:
            return
        async with self._flush_lock:
            rows = self._buffers[table]
            if not rows:
                return
            self._buffers[table] = []
        try:
            await self._repository._client.insert(
                table,
                rows,
                column_names=self._column_names[table],
            )
            self._events_persisted += len(rows)
            self._last_event_time = datetime.now(timezone.utc).isoformat()
        except Exception:
            self._errors += len(rows)
            async with self._flush_lock:
                self._buffers[table][0:0] = rows
            logger.exception(
                "failed to persist Market OS batch",
                extra={"aitos_extra": {"table": table, "rows": len(rows), "event_id": getattr(event, "event_id", None)}},
            )
            if event is not None:
                raise

    def _record_success(self, event: Event) -> None:
        self._last_event_time = datetime.now(timezone.utc).isoformat()


def _symbol_from_topic(topic: str) -> str:
    return topic.rsplit(".", 1)[-1]


def _optional_float(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
