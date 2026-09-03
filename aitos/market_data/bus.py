"""Canonical market-data bus over the existing Redis Streams EventBus.

The adapter deliberately exposes semantic channels and a single envelope. It
keeps the legacy EventBus implementation underneath while preventing new
market-data consumers from creating v2/v3 topic families.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from aitos.core.contracts import Event
from aitos.eventbus.redis_bus import EventBus, Subscription

from .channels import (
    CHANNEL_BOOK_DELTA,
    CHANNEL_BOOK_SNAPSHOT,
    CHANNEL_FUNDING,
    CHANNEL_INSTRUMENT,
    CHANNEL_LIQUIDATION,
    CHANNEL_OPEN_INTEREST,
    CHANNEL_OPTIONS,
    CHANNEL_TICKER,
    CHANNEL_TRADE,
)
from .contracts import MarketEvent, MarketEventType, MarketSource
from .retention import maxlen_for


_CHANNEL_BY_TYPE = {
    MarketEventType.TRADE: CHANNEL_TRADE,
    MarketEventType.BOOK_DELTA: CHANNEL_BOOK_DELTA,
    MarketEventType.BOOK_SNAPSHOT: CHANNEL_BOOK_SNAPSHOT,
    MarketEventType.TICKER: CHANNEL_TICKER,
    MarketEventType.FUNDING: CHANNEL_FUNDING,
    MarketEventType.OPEN_INTEREST: CHANNEL_OPEN_INTEREST,
    MarketEventType.LIQUIDATION: CHANNEL_LIQUIDATION,
    MarketEventType.OPTIONS: CHANNEL_OPTIONS,
    MarketEventType.INSTRUMENT: CHANNEL_INSTRUMENT,
}


def channel_for(event_type: MarketEventType) -> str:
    try:
        return _CHANNEL_BY_TYPE[event_type]
    except KeyError as exc:
        raise ValueError(f"unsupported market event type: {event_type}") from exc


def _datetime_from_wire(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def market_event_to_wire(event: MarketEvent) -> dict[str, Any]:
    """Encode the canonical envelope as a Redis-safe Event payload."""
    return {
        "event_type": event.event_type.value,
        "exchange": event.exchange,
        "market": event.market,
        "symbol": event.symbol,
        "event_time": event.event_time.isoformat(),
        "ingest_time": event.ingest_time.isoformat(),
        "source": event.source.value,
        "event_id": event.event_id,
        "schema_version": event.schema_version,
        "payload": event.payload,
    }


def market_event_from_wire(payload: dict[str, Any]) -> MarketEvent:
    return MarketEvent(
        event_type=MarketEventType(payload["event_type"]),
        exchange=str(payload["exchange"]),
        market=str(payload["market"]),
        symbol=str(payload["symbol"]),
        event_time=_datetime_from_wire(payload["event_time"]),
        payload=dict(payload.get("payload") or {}),
        source=MarketSource(payload["source"]),
        ingest_time=_datetime_from_wire(payload["ingest_time"]),
        event_id=str(payload["event_id"]),
        schema_version=int(payload.get("schema_version", 1)),
    )


MarketEventHandler = Callable[[MarketEvent], Awaitable[Any]]


class MarketDataBus:
    """Semantic market-data API backed by Redis Streams."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus

    async def publish(self, event: MarketEvent) -> None:
        channel = channel_for(event.event_type)
        # The Redis EventBus has configurable channel retention. The explicit
        # maxlen is kept in the payload metadata so persistence/diagnostics can
        # verify the intended retention policy without relying on topic names.
        payload = market_event_to_wire(event)
        payload["retention_maxlen"] = maxlen_for(channel)
        await self._event_bus.publish(
            Event(
                topic=channel,
                payload=payload,
                event_id=event.event_id,
                source_module="market-data-gateway",
                correlation_id=event.event_id,
                schema_version=event.schema_version,
            )
        )

    async def subscribe(
        self,
        event_type: MarketEventType,
        handler: MarketEventHandler,
        *,
        group: str,
        live_only: bool = True,
    ) -> Subscription:
        channel = channel_for(event_type)

        async def _handle(event: Event) -> Any:
            return await handler(market_event_from_wire(event.payload))

        return await self._event_bus.subscribe(
            channel,
            _handle,
            group=group,
            start_id="$" if live_only else "0",
        )
