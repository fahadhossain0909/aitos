from datetime import datetime, timezone

from aitos.intelligence.liquidity import (
    adverse_flow_without_progress,
    liquidity_intelligence_score,
    liquidity_quality_score,
    order_book_flow_imbalance,
)
from aitos.models.market import OrderBookSnapshot, TradeSide, TradeTick


def _book(bid_price, bid_qty, ask_price, ask_qty):
    return OrderBookSnapshot(
        symbol="BTCUSDT",
        bids=((bid_price, bid_qty),),
        asks=((ask_price, ask_qty),),
        last_update_id=1,
        timestamp=datetime.now(timezone.utc),
    )


def _trade(i, price, side):
    return TradeTick(
        symbol="BTCUSDT", trade_id=i, price=price, quantity=1.0,
        side=side, is_buyer_maker=(side == TradeSide.SELL),
    )


def test_order_book_flow_imbalance_detects_bid_pressure():
    previous = _book(100.0, 10.0, 100.5, 10.0)
    current = _book(100.0, 18.0, 100.5, 10.0)
    assert order_book_flow_imbalance(previous, current) > 5.0


def test_order_book_flow_imbalance_is_neutral_without_previous_snapshot():
    assert order_book_flow_imbalance(None, _book(100.0, 10.0, 100.5, 10.0)) == 5.0


def test_liquidity_composite_excludes_directional_and_sweep_risk_scores():
    book = _book(100.0, 10.0, 100.5, 10.0)
    assert liquidity_intelligence_score(book) == liquidity_quality_score(book)


def test_aggressive_buying_without_price_progress_is_flagged_for_long():
    trades = [_trade(i, 100.0 + (i % 2) * 0.001, TradeSide.BUY) for i in range(25)]
    assert adverse_flow_without_progress(trades, "LONG") is True


def test_aggressive_buying_with_clear_price_progress_is_not_flagged():
    trades = [_trade(i, 100.0 + i * 0.02, TradeSide.BUY) for i in range(25)]
    assert adverse_flow_without_progress(trades, "LONG") is False
