import math
from datetime import datetime, timedelta, timezone

from aitos.market_data.contracts import (
    MarketEvent,
    MarketEventType,
    MarketSource,
)
from aitos.spot.lead_lag import FuturesSpotLeadLagAnalyzer


def _ticker(market_type, symbol, price, at):
    return MarketEvent(
        event_type=MarketEventType.TICKER,
        exchange="binance",
        market=market_type,
        market_type=market_type,
        symbol=symbol,
        event_time=at,
        payload={"last_price": str(price)},
        source=MarketSource.REPLAY,
    )


def test_futures_spot_lead_lag_detects_futures_leading_by_two_seconds():
    analyzer = FuturesSpotLeadLagAnalyzer(
        bucket_seconds=1,
        max_lag_seconds=4,
        window_seconds=240,
        min_observations=40,
        min_abs_correlation=0.2,
        min_lead_advantage=0.02,
    )
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    futures_returns = [
        0.0005 * math.sin(index * 1.71)
        + 0.0003 * math.cos(index * 0.37)
        + 0.0001 * math.sin(index * 0.11)
        for index in range(240)
    ]
    futures_prices = [100.0]
    spot_prices = [100.0]
    for index in range(1, 240):
        futures_prices.append(futures_prices[-1] * math.exp(futures_returns[index - 1]))
        delayed_return = futures_returns[index - 3] if index >= 3 else 0.0
        spot_prices.append(spot_prices[-1] * math.exp(delayed_return))

    estimate = None
    for index in range(240):
        at = start + timedelta(seconds=index)
        analyzer.ingest(_ticker("usd_m_futures", "BTCUSDT", futures_prices[index], at))
        estimate = analyzer.ingest(_ticker("spot", "BTCUSDT", spot_prices[index], at))

    assert estimate is not None
    assert estimate.status == "futures_leads"
    assert estimate.leader == "futures"
    assert estimate.lag_seconds == 2
    assert estimate.correlation is not None and estimate.correlation > 0.95
    assert estimate.confidence_low is not None and estimate.confidence_low > 0.8


def test_futures_spot_lead_lag_rejects_unmatched_markets_and_bad_prices():
    analyzer = FuturesSpotLeadLagAnalyzer()
    now = datetime.now(timezone.utc)
    assert analyzer.ingest(_ticker("spot", "BTCUSDT", 0, now)) is None
    assert analyzer.ingest(_ticker("options", "BTCUSDT", 100, now)) is None
    assert analyzer.snapshot()["rejected_events"] == 2


def test_futures_symbol_alias_maps_to_spot_underlying():
    analyzer = FuturesSpotLeadLagAnalyzer(symbol_aliases={"BTCUSDT_UMCBL": "BTCUSDT"})
    now = datetime.now(timezone.utc)
    analyzer.ingest(_ticker("usd_m_futures", "BTCUSDT_UMCBL", 100, now))
    assert "BTCUSDT" in analyzer._pairs
    assert "BTCUSDT_UMCBL" not in analyzer._pairs


def test_futures_spot_synchronous_correlation_is_not_misreported_as_lead():
    analyzer = FuturesSpotLeadLagAnalyzer(
        max_lag_seconds=3,
        window_seconds=180,
        min_observations=30,
        min_abs_correlation=0.2,
    )
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    returns = [
        0.0004 * math.sin(index * 1.37) + 0.0002 * math.cos(index * 0.29)
        for index in range(180)
    ]
    prices = [100.0]
    for value in returns[:-1]:
        prices.append(prices[-1] * math.exp(value))
    estimate = None
    for index, price in enumerate(prices):
        at = start + timedelta(seconds=index)
        analyzer.ingest(_ticker("usd_m_futures", "ETHUSDT", price, at))
        estimate = analyzer.ingest(_ticker("spot", "ETHUSDT", price, at))
    assert estimate is not None
    assert estimate.status == "synchronous"
    assert estimate.leader is None
    assert estimate.lag_seconds is None


def test_futures_spot_lead_lag_is_scoped_to_discovered_spot_universe():
    analyzer = FuturesSpotLeadLagAnalyzer()
    analyzer.set_symbols(["BTCUSDT"])
    now = datetime.now(timezone.utc)
    assert analyzer.ingest(_ticker("usd_m_futures", "ETHUSDT", 100, now)) is None
    assert analyzer.ingest(_ticker("usd_m_futures", "BTCUSDT", 100, now)) is not None
    assert set(analyzer._pairs) == {"BTCUSDT"}


def test_futures_spot_lead_lag_does_not_overwrite_newer_bucket_sample():
    analyzer = FuturesSpotLeadLagAnalyzer()
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer = _ticker("spot", "BTCUSDT", 101, at + timedelta(milliseconds=800))
    older = _ticker("spot", "BTCUSDT", 99, at + timedelta(milliseconds=200))
    analyzer.ingest(newer)
    analyzer.ingest(older)
    sample = analyzer._pairs["BTCUSDT"].spot[int(at.timestamp())]
    assert sample[1] == 101
    assert analyzer.snapshot()["rejected_events"] == 1


def test_futures_spot_lead_lag_estimates_expire_when_market_feed_stales():
    from aitos.spot.lead_lag import LeadLagEstimate

    analyzer = FuturesSpotLeadLagAnalyzer()
    now = datetime.now(timezone.utc)
    analyzer._estimates["BTCUSDT"] = LeadLagEstimate(
        symbol="BTCUSDT",
        status="futures_leads",
        leader="futures",
        lag_seconds=2,
        correlation=0.8,
        confidence_low=0.7,
        confidence_high=0.9,
        observations=100,
        as_of=now - timedelta(seconds=30),
    )
    assert analyzer.estimate("BTCUSDT", max_age_seconds=15) is None

def test_futures_spot_lead_lag_detects_spot_leading_by_two_seconds():
    analyzer = FuturesSpotLeadLagAnalyzer(
        bucket_seconds=1,
        max_lag_seconds=4,
        window_seconds=240,
        min_observations=40,
        min_abs_correlation=0.2,
        min_lead_advantage=0.02,
    )
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    spot_returns = [
        0.0005 * math.sin(index * 1.71)
        + 0.0003 * math.cos(index * 0.37)
        + 0.0001 * math.sin(index * 0.11)
        for index in range(240)
    ]
    spot_prices = [100.0]
    futures_prices = [100.0]
    for index in range(1, 240):
        spot_prices.append(spot_prices[-1] * math.exp(spot_returns[index - 1]))
        delayed_return = spot_returns[index - 3] if index >= 3 else 0.0
        futures_prices.append(futures_prices[-1] * math.exp(delayed_return))

    estimate = None
    for index in range(240):
        at = start + timedelta(seconds=index)
        analyzer.ingest(_ticker("spot", "BTCUSDT", spot_prices[index], at))
        estimate = analyzer.ingest(
            _ticker("usd_m_futures", "BTCUSDT", futures_prices[index], at)
        )

    assert estimate is not None
    assert estimate.status == "spot_leads"
    assert estimate.leader == "spot"
    assert estimate.lag_seconds == 2
    assert estimate.correlation is not None and estimate.correlation > 0.95
    assert estimate.confidence_low is not None and estimate.confidence_low > 0.8
