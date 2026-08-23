from datetime import datetime, timezone

from aitos.intelligence.footprint import FootprintEngine
from aitos.models.market import TradeSide, TradeTick


def trade(price, qty, side, maker=False, trade_id=1):
    return TradeTick(
        symbol="BTCUSDT",
        trade_id=trade_id,
        price=price,
        quantity=qty,
        timestamp=datetime.now(timezone.utc),
        side=side,
        is_buyer_maker=maker,
    )


def test_builds_price_level_bid_ask_volume():
    engine = FootprintEngine(1.0)
    fp = engine.build(
        [
            trade(100.1, 2.0, TradeSide.BUY, trade_id=1),
            trade(100.2, 1.0, TradeSide.SELL, maker=True, trade_id=2),
            trade(100.7, 3.0, TradeSide.BUY, trade_id=3),
        ]
    )
    assert fp is not None
    assert len(fp.levels) == 2
    assert fp.levels[0].price == 100.0
    assert fp.levels[0].ask_volume == 2.0
    assert fp.levels[0].bid_volume == 1.0
    assert fp.total_delta == 4.0


def test_empty_input_returns_none():
    assert FootprintEngine(0.5).build([]) is None


def test_rejects_invalid_tick_size():
    try:
        FootprintEngine(0)
        assert False
    except ValueError:
        pass
