from datetime import datetime, timezone

import pytest

from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.markets.contracts import AssetClass, Instrument
from aitos.markets.instrument_master import InstrumentMaster, InstrumentKey


def _event(*, venue="BINANCE", market="SPOT", symbol="btcusdt"):
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    return MarketEvent(
        event_type=MarketEventType.TICKER,
        exchange=venue,
        market=market,
        symbol=symbol,
        event_time=now,
        payload={"last_price": 100.0},
        source=MarketSource.WEBSOCKET,
    )


def _instrument(symbol="BTCUSDT", market_type="spot", venue="binance"):
    return Instrument(
        symbol=symbol,
        asset_class=AssetClass.CRYPTO,
        venue=venue,
        market_type=market_type,
    )


def test_exact_resolver_normalizes_identity_without_alias_guessing():
    master = InstrumentMaster(
        [_instrument(), _instrument(market_type="usd_m_futures")],
        version="instrument-master-2026-11-19.1",
    )
    assert master(_event()) == _instrument()
    assert master.resolve_event(_event(market="USD_M_FUTURES")) == _instrument(
        market_type="usd_m_futures"
    )
    assert master.resolve_event(_event(symbol="ETHUSDT")) is None
    assert len(master) == 2
    assert master.version == "instrument-master-2026-11-19.1"


def test_same_symbol_across_market_types_is_not_ambiguous():
    spot = _instrument(market_type="spot")
    futures = _instrument(market_type="usd_m_futures")
    master = InstrumentMaster([spot, futures], version="v1")
    assert master.resolve(venue="binance", market_type="spot", symbol="BTCUSDT") is spot
    assert master.resolve(
        venue="binance", market_type="usd_m_futures", symbol="BTCUSDT"
    ) is futures


def test_duplicate_normalized_identity_is_rejected():
    with pytest.raises(ValueError, match="duplicate instrument identity"):
        InstrumentMaster(
            [_instrument(), _instrument(symbol="btcusdt", venue="BINANCE")],
            version="v1",
        )


def test_incomplete_or_invalid_master_rows_are_rejected():
    with pytest.raises(ValueError, match="market_type"):
        InstrumentMaster(
            [Instrument("BTCUSDT", AssetClass.CRYPTO, "binance")], version="v1"
        )
    with pytest.raises(ValueError, match="version"):
        InstrumentMaster([_instrument()], version=" ")
    with pytest.raises(TypeError, match="Instrument"):
        InstrumentMaster([object()], version="v1")


def test_resolver_does_not_infer_missing_market_or_venue():
    master = InstrumentMaster([_instrument()], version="v1")
    assert master.resolve(venue="binance", market_type="", symbol="BTCUSDT") is None
    with pytest.raises(ValueError, match="market_type"):
        master.resolve(venue="binance", market_type="", symbol="BTCUSDT")


def test_instrument_key_is_stable_and_case_normalized():
    assert InstrumentKey.from_values(" Binance ", "SPOT", "btcusdt") == (
        InstrumentKey("binance", "spot", "BTCUSDT")
    )
