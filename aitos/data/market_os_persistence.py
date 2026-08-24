"""Durable persistence for Market OS live-state and order-flow events."""

from __future__ import annotations

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

CREATE_MARKET_ORDERFLOW = """
CREATE TABLE IF NOT EXISTS market_orderflow (
    time DateTime64(3, 'UTC'),
    event_id String,
    symbol LowCardinality(String),
    trade_count UInt64,
    buy_volume Float64,
    sell_volume Float64,
    delta Float64,
    cvd Float64,
    buy_ratio Float64,
    aggression Float64,
    imbalance Float64,
    vwap Float64,
    last_price Float64,
    direction String
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(time)
ORDER BY (symbol, time, event_id)
"""

CREATE_MARKET_LIQUIDITY = """
CREATE TABLE IF NOT EXISTS market_liquidity_events (
    time DateTime64(3, 'UTC'),
    event_id String,
    symbol LowCardinality(String),
    kind LowCardinality(String),
    side LowCardinality(String),
    score Float64,
    price Float64,
    details_json String,
    last_update_id UInt64
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(time)
ORDER BY (symbol, time, event_id)
"""

CREATE_MARKET_LIVE_STATE = """
CREATE TABLE IF NOT EXISTS market_live_state (
    time DateTime64(3, 'UTC'),
    event_id String,
    symbol LowCardinality(String),
    trade_count UInt64,
    order_flow_json String,
    liquidity_events_json String,
    best_bid Nullable(Float64),
    best_ask Nullable(Float64),
    state_timestamp Nullable(DateTime64(3, 'UTC'))
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(time)
ORDER BY (symbol, time, event_id)
"""


class MarketOSPersistence(AITOSModule):
    """Persist Market OS events without coupling the live state store to ClickHouse."""

    def __init__(
        self, event_bus: EventBus, repository: Optional[MarketDataRepository]
    ) -> None:
        self._event_bus = event_bus
        self._repository = repository
        self._subscriptions: list[Subscription] = []
        self._initialized = False
        self._events_persisted = 0
        self._errors = 0
        self._last_event_time: Optional[str] = None

    @property
    def module_id(self) -> str:
        return "market-os-persistence"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: Dict[str, Any]) -> None:
        if self._initialized:
            return
        if self._repository is None:
            logger.warning(
                "Market OS persistence disabled: ClickHouse repository unavailable"
            )
            self._initialized = True
            return
        await self._repository._client.command(CREATE_MARKET_ORDERFLOW)
        await self._repository._client.command(CREATE_MARKET_LIQUIDITY)
        await self._repository._client.command(CREATE_MARKET_LIVE_STATE)
        self._subscriptions = [
            await self._event_bus.subscribe(
                "market.orderflow.*",
                self._handle_orderflow,
                group="market-os-orderflow",
            ),
            await self._event_bus.subscribe(
                "market.liquidity.*",
                self._handle_liquidity,
                group="market-os-liquidity",
            ),
            await self._event_bus.subscribe(
                "market.live_state.*",
                self._handle_live_state,
                group="market-os-live-state",
            ),
        ]
        self._initialized = True
        logger.info("Market OS persistence initialized")

    async def health_check(self) -> HealthStatus:
        status = (
            ModuleStatus.HEALTHY
            if self._repository is not None
            else ModuleStatus.DEGRADED
        )
        return HealthStatus(
            module_id=self.module_id,
            status=status,
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={
                "events_persisted": self._events_persisted,
                "errors": self._errors,
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions.clear()

    async def emit_events(self):
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> Optional[EventResponse]:
        return None

    async def _handle_orderflow(self, event: Event) -> Optional[EventResponse]:
        payload = event.payload
        symbol = _symbol_from_topic(event.topic)
        try:
            await self._repository._client.insert(
                "market_orderflow",
                [
                    [
                        event.created_at,
                        event.event_id,
                        symbol,
                        int(payload.get("trade_count", 0)),
                        float(payload.get("buy_volume", 0.0)),
                        float(payload.get("sell_volume", 0.0)),
                        float(payload.get("delta", 0.0)),
                        float(payload.get("cvd", 0.0)),
                        float(payload.get("buy_ratio", 0.0)),
                        float(payload.get("aggression", 0.0)),
                        float(payload.get("imbalance", 0.0)),
                        float(payload.get("vwap", 0.0)),
                        float(payload.get("last_price", 0.0)),
                        str(payload.get("direction", "")),
                    ]
                ],
                column_names=[
                    "time",
                    "event_id",
                    "symbol",
                    "trade_count",
                    "buy_volume",
                    "sell_volume",
                    "delta",
                    "cvd",
                    "buy_ratio",
                    "aggression",
                    "imbalance",
                    "vwap",
                    "last_price",
                    "direction",
                ],
            )
            self._record_success(event)
        except Exception:
            self._record_error(event, "orderflow")
            raise
        return None

    async def _handle_liquidity(self, event: Event) -> Optional[EventResponse]:
        payload = event.payload
        symbol = _symbol_from_topic(event.topic)
        try:
            await self._repository._client.insert(
                "market_liquidity_events",
                [
                    [
                        event.created_at,
                        event.event_id,
                        symbol,
                        str(payload.get("kind", "")),
                        str(payload.get("side", "")),
                        float(payload.get("score", 0.0)),
                        float(payload.get("price", 0.0)),
                        json.dumps(
                            payload.get("details", {}), sort_keys=True, default=str
                        ),
                        int(payload.get("last_update_id", 0)),
                    ]
                ],
                column_names=[
                    "time",
                    "event_id",
                    "symbol",
                    "kind",
                    "side",
                    "score",
                    "price",
                    "details_json",
                    "last_update_id",
                ],
            )
            self._record_success(event)
        except Exception:
            self._record_error(event, "liquidity")
            raise
        return None

    async def _handle_live_state(self, event: Event) -> Optional[EventResponse]:
        payload = event.payload
        symbol = _symbol_from_topic(event.topic)
        state_timestamp = _parse_timestamp(payload.get("timestamp"))
        try:
            await self._repository._client.insert(
                "market_live_state",
                [
                    [
                        event.created_at,
                        event.event_id,
                        symbol,
                        int(payload.get("trade_count", 0)),
                        json.dumps(
                            payload.get("order_flow"), sort_keys=True, default=str
                        ),
                        json.dumps(
                            payload.get("liquidity_events", []),
                            sort_keys=True,
                            default=str,
                        ),
                        _optional_float(payload.get("best_bid")),
                        _optional_float(payload.get("best_ask")),
                        state_timestamp,
                    ]
                ],
                column_names=[
                    "time",
                    "event_id",
                    "symbol",
                    "trade_count",
                    "order_flow_json",
                    "liquidity_events_json",
                    "best_bid",
                    "best_ask",
                    "state_timestamp",
                ],
            )
            self._record_success(event)
        except Exception:
            self._record_error(event, "live_state")
            raise
        return None

    def _record_success(self, event: Event) -> None:
        self._events_persisted += 1
        self._last_event_time = datetime.now(timezone.utc).isoformat()

    def _record_error(self, event: Event, stream: str) -> None:
        self._errors += 1
        logger.exception(
            "failed to persist Market OS event",
            extra={
                "aitos_extra": {
                    "stream": stream,
                    "topic": event.topic,
                    "event_id": event.event_id,
                }
            },
        )


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
