"""Explicit compatibility bridges between market-data and market-domain contracts.

The market-data bus remains the canonical ingress contract. This module projects
an event into the asset-class-neutral market domain only when the caller supplies
an already-resolved Instrument; it never guesses asset class or contract metadata.
"""

from __future__ import annotations

from aitos.market_data.contracts import (
    MarketEvent as CanonicalMarketDataEvent,
    MarketEventType as CanonicalMarketEventType,
)
from aitos.markets.contracts import Instrument, MarketEvent, MarketEventType

_EVENT_TYPE_MAP = {
    CanonicalMarketEventType.TRADE: MarketEventType.TRADE,
    CanonicalMarketEventType.BOOK_DELTA: MarketEventType.ORDER_BOOK,
    CanonicalMarketEventType.BOOK_SNAPSHOT: MarketEventType.ORDER_BOOK,
    CanonicalMarketEventType.TICKER: MarketEventType.QUOTE,
    CanonicalMarketEventType.KLINE: MarketEventType.BAR,
    CanonicalMarketEventType.FUNDING: MarketEventType.FUNDING,
    CanonicalMarketEventType.OPEN_INTEREST: MarketEventType.OPEN_INTEREST,
    CanonicalMarketEventType.LIQUIDATION: MarketEventType.LIQUIDATION,
    CanonicalMarketEventType.OPTIONS: MarketEventType.OPTIONS,
    CanonicalMarketEventType.INSTRUMENT: MarketEventType.INSTRUMENT,
}


def market_data_event_to_domain(
    event: CanonicalMarketDataEvent,
    instrument: Instrument,
) -> MarketEvent:
    """Project a canonical bus event into the universal market-domain contract.

    The instrument must come from the instrument master/resolver. Venue and
    symbol are checked so this adapter cannot silently attach an event to a
    different asset. Original event type and transport provenance are retained
    as first-class fields, including the distinction between book deltas and
    snapshots (both share the domain ORDER_BOOK family).
    """
    if not isinstance(event, CanonicalMarketDataEvent):
        raise TypeError("event must be a canonical market-data MarketEvent")
    if not isinstance(instrument, Instrument):
        raise TypeError("instrument must be a resolved markets.Instrument")

    if event.symbol.strip().upper() != instrument.symbol.strip().upper():
        raise ValueError("resolved instrument symbol does not match market event")
    event_venue = str(event.venue or event.exchange).strip().lower()
    if event_venue != instrument.venue.strip().lower():
        raise ValueError("resolved instrument venue does not match market event")

    try:
        domain_type = _EVENT_TYPE_MAP[event.event_type]
    except KeyError as exc:
        raise ValueError(
            f"unsupported canonical market event type: {event.event_type}"
        ) from exc

    return MarketEvent(
        event_type=domain_type,
        instrument=instrument,
        event_time=event.event_time,
        payload=dict(event.payload),
        source=event.source.value,
        received_at=event.ingest_time,
        event_id=event.event_id,
        schema_version=event.schema_version,
        correlation_id=event.correlation_id,
        event_revision=event.event_revision,
        sequence=event.sequence,
        trace_id=event.trace_id,
        venue=event.venue or event.exchange,
        market_type=event.market_type or event.market,
        source_event_type=event.event_type.value,
        stage_timestamps=dict(event.stage_timestamps),
    )
