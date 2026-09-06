from datetime import datetime, timedelta, timezone

from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.market_data.telemetry import MarketDataTelemetry


def test_market_event_keeps_source_and_computes_source_age() -> None:
    event_time = datetime.now(timezone.utc) - timedelta(seconds=2)
    event = MarketEvent(
        event_type=MarketEventType.TRADE,
        exchange="binance",
        market="usd_m_futures",
        symbol="BTCUSDT",
        event_time=event_time,
        payload={"price": 100.0},
        source=MarketSource.REST,
    )
    assert event.source is MarketSource.REST
    assert event.source_age_seconds >= 1.0


def test_telemetry_is_bounded_to_named_streams() -> None:
    telemetry = MarketDataTelemetry()
    stream = telemetry.stream("binance.usdm.trade")
    stream.mark_received(1_000)
    stream.parsed += 1
    stream.published += 1
    snapshot = telemetry.snapshot()
    assert snapshot["binance.usdm.trade"]["received"] == 1
    assert snapshot["binance.usdm.trade"]["parsed"] == 1
