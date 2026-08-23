from datetime import datetime, timezone

from aitos.intelligence.auction import auction_context_score
from aitos.intelligence.order_flow import (buy_volume_ratio, delta,
                                           order_flow_bias_score)
from aitos.models.market import Kline, TradeSide, TradeTick


def _trade(qty, buyer_maker=False):
    return TradeTick(
        symbol="BTCUSDT",
        trade_id=1,
        price=100.0,
        quantity=qty,
        side=TradeSide.BUY if not buyer_maker else TradeSide.SELL,
        is_buyer_maker=buyer_maker,
        timestamp=datetime.now(timezone.utc),
    )


def test_real_trade_flow_is_buy_heavy():
    trades = [_trade(3), _trade(2), _trade(1, buyer_maker=True)]
    assert delta(trades) == 4
    assert buy_volume_ratio(trades) == 5 / 6
    assert order_flow_bias_score(trades) > 8


def test_real_trade_flow_is_sell_heavy():
    trades = [_trade(1), _trade(4, buyer_maker=True), _trade(2, buyer_maker=True)]
    assert delta(trades) == -5
    assert order_flow_bias_score(trades) < 2


def test_auction_context_is_bounded():
    now = datetime.now(timezone.utc)
    klines = [
        Kline(
            "BTCUSDT",
            "15m",
            now,
            now,
            100 + i,
            102 + i,
            99 + i,
            101 + i,
            100,
            100,
            10,
            55,
            55,
        )
        for i in range(20)
    ]
    assert 0 <= auction_context_score(klines, "long") <= 10
    assert 0 <= auction_context_score(klines, "short") <= 10
