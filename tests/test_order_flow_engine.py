from datetime import datetime, timezone

from aitos.intelligence.order_flow_engine import OrderFlowEngine
from aitos.models.market import TradeSide, TradeTick


def trade(i, price, qty, side, maker=False):
    return TradeTick(
        symbol="BTCUSDT",
        trade_id=i,
        price=price,
        quantity=qty,
        side=side,
        is_buyer_maker=maker,
        timestamp=datetime.now(timezone.utc),
    )


def test_live_and_batch_paths_produce_same_features():
    trades = [
        trade(1, 100.0, 2.0, TradeSide.BUY),
        trade(2, 101.0, 1.0, TradeSide.SELL, maker=True),
        trade(3, 102.0, 3.0, TradeSide.BUY),
    ]
    live = OrderFlowEngine()
    for t in trades:
        live.ingest(t)
    batch = OrderFlowEngine()
    batch.ingest_many(trades)
    assert live.features() == batch.features()


def test_delta_cvd_and_direction():
    engine = OrderFlowEngine()
    engine.ingest(trade(1, 100, 4, TradeSide.BUY))
    engine.ingest(trade(2, 100, 1, TradeSide.SELL, maker=True))
    f = engine.features()
    assert f.delta == 3
    assert f.cvd == 3
    assert f.direction == "long"
    assert f.buy_ratio == 0.8


def test_empty_engine_is_neutral():
    f = OrderFlowEngine().features()
    assert f.trade_count == 0
    assert f.direction == "neutral"
    assert f.imbalance == 5.0


def test_window_is_bounded():
    engine = OrderFlowEngine(max_trades=2)
    engine.ingest(trade(1, 100, 1, TradeSide.BUY))
    engine.ingest(trade(2, 101, 1, TradeSide.BUY))
    engine.ingest(trade(3, 102, 1, TradeSide.SELL, maker=True))
    assert engine.features().trade_count == 2


def test_cvd_is_bounded_to_the_same_window_as_rolling_trades():
    engine = OrderFlowEngine(max_trades=2)
    engine.ingest(trade(1, 100.0, 1.0, TradeSide.BUY))
    engine.ingest(trade(2, 100.0, 1.0, TradeSide.BUY))
    engine.ingest(trade(3, 100.0, 1.0, TradeSide.SELL, maker=True))
    assert len(engine.trades) == 2
    assert engine.snapshot().cvd == 0.0
