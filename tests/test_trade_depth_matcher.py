from datetime import datetime, timedelta, timezone

from aitos.intelligence.liquidity import depth_event_flow_analysis
from aitos.intelligence.trade_depth_matcher import TradeDepthMatcher
from aitos.models.market import TradeSide, TradeTick


BASE = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _trade(
    trade_id: int,
    *,
    side: TradeSide = TradeSide.SELL,
    price: float = 100.0,
    quantity: float = 1.0,
    offset_ms: int = 0,
) -> TradeTick:
    return TradeTick(
        symbol="BTCUSDT",
        trade_id=trade_id,
        price=price,
        quantity=quantity,
        side=side,
        is_buyer_maker=side is TradeSide.SELL,
        timestamp=BASE + timedelta(milliseconds=offset_ms),
    )


def _depth_event(*, side="bid", price=100.0, quantity=5.0, offset_ms=0):
    timestamp_ms = int((BASE + timedelta(milliseconds=offset_ms)).timestamp() * 1000)
    return {
        "event_time_ms": timestamp_ms,
        "reduction_qty": quantity,
        "inferred_cancel_qty": quantity,
        "reductions": [
            {"side": side, "price": price, "quantity": quantity}
        ],
    }


def test_trade_before_depth_is_matched_by_price_side_and_time():
    matcher = TradeDepthMatcher()
    matcher.add_trade(_trade(1, quantity=3.0, offset_ms=200))
    event = matcher.add_depth_event(_depth_event(quantity=5.0, offset_ms=100))

    assert event["matched_execution_qty"] == 3.0
    assert event["unmatched_reduction_qty"] == 2.0
    assert event["cancellation_proxy_qty"] == 0.0
    assert event["attribution_pending_qty"] == 2.0
    assert event["execution_attributed_ratio"] == 0.6


def test_only_aggressor_trade_on_correct_side_and_price_is_attributed():
    matcher = TradeDepthMatcher()
    matcher.add_depth_event(_depth_event(quantity=5.0))
    matcher.add_trade(_trade(1, side=TradeSide.BUY, quantity=5.0))
    matcher.add_trade(_trade(2, side=TradeSide.SELL, price=100.5, quantity=5.0))
    event = matcher.add_depth_event(_depth_event(side="ask", price=100.5, quantity=2.0))

    bid_event = matcher._reductions[0]["event"]
    assert bid_event["matched_execution_qty"] == 0.0
    assert event["matched_execution_qty"] == 0.0


def test_unmatched_reduction_becomes_cancellation_proxy_only_after_window():
    matcher = TradeDepthMatcher(match_window_ms=1_000)
    event = matcher.add_depth_event(_depth_event(quantity=5.0))
    assert event["cancellation_proxy_qty"] == 0.0
    matcher.add_trade(_trade(1, price=101.0, offset_ms=1_001))

    assert event["attribution_complete"] is True
    assert event["attribution_pending_qty"] == 0.0
    assert event["cancellation_proxy_qty"] == 5.0


def test_trade_quantity_cannot_be_double_counted_across_depth_reductions():
    matcher = TradeDepthMatcher()
    first = matcher.add_depth_event(_depth_event(quantity=4.0))
    second = matcher.add_depth_event(_depth_event(quantity=4.0, offset_ms=100))
    matcher.add_trade(_trade(1, quantity=5.0, offset_ms=50))

    assert first["matched_execution_qty"] + second["matched_execution_qty"] == 5.0



def test_execution_matched_reduction_is_not_reported_as_cancellation():
    matcher = TradeDepthMatcher()
    event = matcher.add_depth_event(_depth_event(quantity=5.0))
    matcher.add_trade(_trade(1, quantity=3.0, offset_ms=200))
    result = depth_event_flow_analysis([event])

    assert result["matched_execution_qty"] == 3.0
    assert result["cancellation_proxy_qty"] == 0.0
    assert result["attribution_pending_qty"] == 2.0


def test_finalized_unmatched_quantity_is_reported_as_cancellation_proxy():
    matcher = TradeDepthMatcher()
    event = matcher.add_depth_event(_depth_event(quantity=5.0))
    matcher.add_trade(_trade(1, price=101.0, offset_ms=1_001))
    result = depth_event_flow_analysis([event])

    assert result["matched_execution_qty"] == 0.0
    assert result["cancellation_proxy_qty"] == 5.0
    assert result["inferred_cancel_qty"] == 5.0



def test_aggressive_buy_matches_ask_reduction():
    matcher = TradeDepthMatcher()
    event = matcher.add_depth_event(
        _depth_event(side="ask", price=100.5, quantity=2.0)
    )
    matcher.add_trade(
        _trade(
            1,
            side=TradeSide.BUY,
            price=100.5,
            quantity=2.0,
            offset_ms=100,
        )
    )

    assert event["matched_execution_qty"] == 2.0
    assert event["unmatched_reduction_qty"] == 0.0
