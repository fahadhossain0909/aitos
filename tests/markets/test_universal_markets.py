from datetime import datetime, timedelta, timezone

import pytest

from aitos.markets import (
    AssetClass,
    CrossMarketIntelligenceEngine,
    ExecutionIntent,
    GlobalMarketState,
    Instrument,
    MarketEvent,
    MarketEventType,
    MarketRegime,
    Portfolio,
    Position,
    RiskEngine,
)


def instrument(symbol: str, asset_class: AssetClass) -> Instrument:
    return Instrument(symbol=symbol, asset_class=asset_class, venue="test")


def event(inst: Instrument, price: float) -> MarketEvent:
    now = datetime.now(timezone.utc)
    return MarketEvent(MarketEventType.TRADE, inst, now, {"price": price}, "test", now)


def test_instrument_identity_is_asset_class_neutral():
    assert instrument("BTCUSD", AssetClass.CRYPTO).id == "test:crypto:BTCUSD"
    assert instrument("AAPL", AssetClass.EQUITY).id == "test:equity:AAPL"
    assert instrument("EURUSD", AssetClass.FOREX).id == "test:forex:EURUSD"


def test_cross_market_engine_can_compare_different_asset_classes():
    btc = instrument("BTCUSD", AssetClass.CRYPTO)
    nasdaq = instrument("NDX", AssetClass.INDEX)
    engine = CrossMarketIntelligenceEngine()
    for i in range(40):
        engine.ingest_price(event(btc, 100 + i))
        engine.ingest_price(event(nasdaq, 200 + 2 * i))
    result = engine.discover_lead_lag(nasdaq.id, btc.id, max_lag=3)
    assert result.observations >= 30
    assert -1.0 <= result.correlation <= 1.0


def test_portfolio_aggregates_by_asset_class():
    portfolio = Portfolio(cash=10_000)
    btc = instrument("BTCUSD", AssetClass.CRYPTO)
    aapl = instrument("AAPL", AssetClass.EQUITY)
    portfolio.upsert(Position(btc, 1, 100, 110))
    portfolio.upsert(Position(aapl, 10, 100, 105))
    exposure = portfolio.exposure_by_asset_class()
    assert exposure["crypto"] == 110
    assert exposure["equity"] == 1050


def test_risk_gate_blocks_excessive_notional():
    portfolio = Portfolio()
    state = GlobalMarketState(regime=MarketRegime.LOW_VOLATILITY, liquidity_score=0.8)
    engine = RiskEngine(max_gross_leverage=2.0, max_single_position_fraction=0.25)
    decision = engine.evaluate(
        instrument=instrument("EURUSD", AssetClass.FOREX),
        requested_notional=3000,
        portfolio=portfolio,
        state=state,
        equity=10_000,
    )
    assert not decision.allowed
    assert decision.max_notional == 2500


def test_high_volatility_reduces_allowed_size():
    portfolio = Portfolio()
    state = GlobalMarketState(volatility_score=0.9, liquidity_score=0.9)
    engine = RiskEngine(max_gross_leverage=2.0, max_single_position_fraction=0.5)
    decision = engine.evaluate(
        instrument=instrument("CL", AssetClass.COMMODITY),
        requested_notional=3000,
        portfolio=portfolio,
        state=state,
        equity=10_000,
    )
    assert decision.max_notional == 1250


def test_execution_intent_is_the_strategy_execution_boundary():
    intent = ExecutionIntent(
        instrument=instrument("AAPL", AssetClass.EQUITY),
        side="buy",
        target_quantity=2,
        max_slippage_bps=10,
    )
    assert intent.instrument.asset_class is AssetClass.EQUITY
    assert intent.side == "buy"


def test_invalid_execution_intent_is_rejected():
    with pytest.raises(ValueError):
        ExecutionIntent(instrument("BTCUSD", AssetClass.CRYPTO), "hold", 1)


def test_timestamp_aligned_lead_lag_uses_utc_buckets_not_sample_indices():
    leader = instrument("BTCUSD", AssetClass.CRYPTO)
    follower = instrument("NDX", AssetClass.INDEX)
    engine = CrossMarketIntelligenceEngine(max_samples=256)
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    returns = [(((index * 17) % 31) - 15) / 100_000 for index in range(1, 61)]
    leader_price = 100.0
    follower_price = 200.0
    leader_prices = [leader_price]
    follower_prices = [follower_price]
    for index, change in enumerate(returns):
        leader_price *= 1.0 + change
        leader_prices.append(leader_price)
        follower_change = returns[index - 2] if index >= 2 else 0.0007 * (index + 1)
        follower_price *= 1.0 + follower_change
        follower_prices.append(follower_price)

    for index, price in enumerate(leader_prices):
        timestamp = start + timedelta(seconds=index)
        engine.ingest_price(
            MarketEvent(
                MarketEventType.TRADE,
                leader,
                timestamp,
                {"price": price},
                "test",
                timestamp,
            )
        )
    for index, price in enumerate(follower_prices):
        timestamp = start + timedelta(seconds=index)
        engine.ingest_price(
            MarketEvent(
                MarketEventType.TRADE,
                follower,
                timestamp,
                {"price": price},
                "test",
                timestamp,
            )
        )

    result = engine.discover_timestamp_aligned_lead_lag(
        leader.id,
        follower.id,
        max_lag_buckets=4,
        bucket_seconds=1.0,
        min_observations=20,
    )
    assert result.lag_steps == 2
    assert result.observations >= 50
    assert result.correlation == pytest.approx(1.0, abs=1e-8)


def test_timestamped_returns_do_not_bridge_missing_buckets():
    symbol = instrument("BTCUSD", AssetClass.CRYPTO)
    engine = CrossMarketIntelligenceEngine()
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for second, price in ((0, 100.0), (1, 101.0), (3, 103.0), (4, 104.0)):
        timestamp = start + timedelta(seconds=second)
        engine.ingest_price(
            MarketEvent(
                MarketEventType.TRADE,
                symbol,
                timestamp,
                {"price": price},
                "test",
                timestamp,
            )
        )
    returns = engine.timestamped_returns(symbol.id, bucket_seconds=1.0)
    assert set(returns) == {int(start.timestamp()) + 1, int(start.timestamp()) + 4}


def test_timestamp_aligned_lead_lag_rejects_invalid_lag_and_bucket():
    symbol = instrument("BTCUSD", AssetClass.CRYPTO)
    engine = CrossMarketIntelligenceEngine()
    with pytest.raises(ValueError, match="lag_buckets"):
        engine.timestamp_aligned_correlation(symbol.id, symbol.id, lag_buckets=-1)
    with pytest.raises(ValueError, match="lag_buckets"):
        engine.timestamp_aligned_correlation(symbol.id, symbol.id, lag_buckets=True)
    with pytest.raises(ValueError, match="bucket_seconds"):
        engine.timestamped_returns(symbol.id, bucket_seconds=0)
