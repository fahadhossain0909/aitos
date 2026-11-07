from datetime import datetime, timezone

from aitos.intelligence.liquidity import (
    adverse_flow_without_progress,
    liquidity_intelligence_score,
    liquidity_quality_score,
    order_book_flow_imbalance,
)
from aitos.intelligence.scanner import entry_liquidity_depth_rejection
from aitos.models.market import OrderBookSnapshot, TradeSide, TradeTick
from aitos.models.trade import TradeSide as PositionSide


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
        symbol="BTCUSDT",
        trade_id=i,
        price=price,
        quantity=1.0,
        side=side,
        is_buyer_maker=(side == TradeSide.SELL),
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


def test_liquidity_floor_is_a_hard_entry_gate():
    result = entry_liquidity_depth_rejection(
        direction=PositionSide.LONG,
        liquidity_quality=4.99,
        depth_analysis={"ofi_available": False},
    )
    assert result is not None
    assert result[0] == "liquidity_quality_below_floor"


def test_cancel_pressure_veto_is_direction_aware_and_requires_fresh_depth():
    depth = {
        "ofi_available": True,
        "bid_inferred_cancel_pressure": 0.61,
        "ask_inferred_cancel_pressure": 0.20,
        "ofi_confidence": 0.8,
        "freshness_ms": 100.0,
    }
    long_result = entry_liquidity_depth_rejection(
        direction=PositionSide.LONG, liquidity_quality=5.0, depth_analysis=depth
    )
    short_result = entry_liquidity_depth_rejection(
        direction=PositionSide.SHORT, liquidity_quality=5.0, depth_analysis=depth
    )
    assert long_result is not None
    assert long_result[0] == "directional_cancel_pressure_too_high"
    assert short_result is None


def test_short_cancel_pressure_uses_ask_side_and_stale_depth_does_not_veto():
    depth = {
        "ofi_available": True,
        "bid_inferred_cancel_pressure": 0.15,
        "ask_inferred_cancel_pressure": 0.75,
    }
    short_result = entry_liquidity_depth_rejection(
        direction=PositionSide.SHORT, liquidity_quality=6.0, depth_analysis=depth
    )
    assert short_result is not None
    assert short_result[1]["cancel_pressure_side"] == "ask"

    stale_result = entry_liquidity_depth_rejection(
        direction=PositionSide.LONG,
        liquidity_quality=6.0,
        depth_analysis={
            "ofi_available": False,
            "bid_inferred_cancel_pressure": 1.0,
        },
    )
    assert stale_result is None


def test_aggressive_buying_without_price_progress_is_flagged_for_long():
    trades = [_trade(i, 100.0 + (i % 2) * 0.001, TradeSide.BUY) for i in range(25)]
    assert adverse_flow_without_progress(trades, "LONG") is True


def test_aggressive_buying_with_clear_price_progress_is_not_flagged():
    trades = [_trade(i, 100.0 + i * 0.02, TradeSide.BUY) for i in range(25)]
    assert adverse_flow_without_progress(trades, "LONG") is False
