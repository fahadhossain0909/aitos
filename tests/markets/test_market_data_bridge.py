from datetime import datetime, timedelta, timezone

import pytest

from aitos.market_data.contracts import (
    MarketEvent as CanonicalMarketDataEvent,
    MarketEventType as CanonicalMarketEventType,
    MarketSource,
)
from aitos.markets.bridges import market_data_event_to_domain
from aitos.markets.contracts import AssetClass, Instrument, MarketEventType


@pytest.fixture
def instrument():
    return Instrument(
        symbol="BTCUSDT",
        asset_class=AssetClass.CRYPTO,
        venue="binance",
        settlement_type="perpetual",
        margin_model="cross",
    )


def event(event_type=CanonicalMarketEventType.TRADE):
    now = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    return CanonicalMarketDataEvent(
        event_type=event_type,
        exchange="binance",
        market="usd_m_futures",
        symbol="BTCUSDT",
        event_time=now,
        payload={"price": 90000.0, "quantity": 0.1},
        source=MarketSource.WEBSOCKET,
        ingest_time=now + timedelta(milliseconds=25),
        event_id="evt-123",
        schema_version=2,
        event_revision=3,
        venue="binance",
        market_type="usd_m_futures",
        sequence=9876,
        correlation_id="corr-123",
        trace_id="md:BTCUSDT:evt-123",
        stage_timestamps={"decode": 1.0, "normalize": 2.0},
    )


def test_bridge_preserves_identity_timing_and_provenance(instrument):
    source = event()
    domain = market_data_event_to_domain(source, instrument)

    assert domain.event_type is MarketEventType.TRADE
    assert domain.instrument is instrument
    assert domain.instrument.id == "binance:crypto:BTCUSDT"
    assert domain.event_id == source.event_id
    assert domain.event_time == source.event_time
    assert domain.received_at == source.ingest_time
    assert domain.source == "websocket"
    assert domain.schema_version == 2
    assert domain.event_revision == 3
    assert domain.sequence == 9876
    assert domain.correlation_id == "corr-123"
    assert domain.trace_id == "md:BTCUSDT:evt-123"
    assert domain.venue == "binance"
    assert domain.market_type == "usd_m_futures"
    assert domain.source_event_type == "trade"
    assert domain.source_instrument_id == "BTCUSDT"
    assert domain.stage_timestamps == {"decode": 1.0, "normalize": 2.0}
    assert domain.payload == source.payload
    assert domain.payload is not source.payload


@pytest.mark.parametrize(
    ("source_type", "domain_type"),
    [
        (CanonicalMarketEventType.TRADE, MarketEventType.TRADE),
        (CanonicalMarketEventType.BOOK_DELTA, MarketEventType.ORDER_BOOK),
        (CanonicalMarketEventType.BOOK_SNAPSHOT, MarketEventType.ORDER_BOOK),
        (CanonicalMarketEventType.TICKER, MarketEventType.QUOTE),
        (CanonicalMarketEventType.KLINE, MarketEventType.BAR),
        (CanonicalMarketEventType.FUNDING, MarketEventType.FUNDING),
        (CanonicalMarketEventType.OPEN_INTEREST, MarketEventType.OPEN_INTEREST),
        (CanonicalMarketEventType.LIQUIDATION, MarketEventType.LIQUIDATION),
        (CanonicalMarketEventType.OPTIONS, MarketEventType.OPTIONS),
        (CanonicalMarketEventType.INSTRUMENT, MarketEventType.INSTRUMENT),
    ],
)
def test_bridge_maps_every_canonical_event_family(instrument, source_type, domain_type):
    result = market_data_event_to_domain(event(source_type), instrument)
    assert result.event_type is domain_type
    assert result.source_event_type == source_type.value


def test_bridge_keeps_book_delta_and_snapshot_distinguishable(instrument):
    delta = market_data_event_to_domain(
        event(CanonicalMarketEventType.BOOK_DELTA), instrument
    )
    snapshot = market_data_event_to_domain(
        event(CanonicalMarketEventType.BOOK_SNAPSHOT), instrument
    )
    assert delta.event_type is snapshot.event_type is MarketEventType.ORDER_BOOK
    assert delta.source_event_type == "book_delta"
    assert snapshot.source_event_type == "book_snapshot"


def test_bridge_rejects_symbol_or_venue_mismatch(instrument):
    wrong_symbol = Instrument("ETHUSDT", AssetClass.CRYPTO, "binance")
    wrong_venue = Instrument("BTCUSDT", AssetClass.CRYPTO, "bybit")
    with pytest.raises(ValueError, match="symbol"):
        market_data_event_to_domain(event(), wrong_symbol)
    with pytest.raises(ValueError, match="venue"):
        market_data_event_to_domain(event(), wrong_venue)


def test_bridge_requires_explicit_resolved_instrument():
    with pytest.raises(TypeError, match="resolved markets.Instrument"):
        market_data_event_to_domain(event(), object())
