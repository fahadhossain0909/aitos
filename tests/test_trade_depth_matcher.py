from datetime import datetime, timedelta, timezone

import pytest

from aitos.intelligence.liquidity import depth_event_flow_analysis
from aitos.intelligence.live_scanner import LiveScannerCache
from aitos.intelligence.trade_depth_matcher import TradeDepthMatcher
from aitos.exchange.orderbook import DepthUpdate, LocalOrderBook
from aitos.models.market import OrderBookSnapshot, TradeSide, TradeTick

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
        "reductions": [{"side": side, "price": price, "quantity": quantity}],
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
    result = depth_event_flow_analysis([event], now_ms=event["event_time_ms"])

    assert result["matched_execution_qty"] == 3.0
    assert result["matched_bid_execution_qty"] == 3.0
    assert result["cancellation_proxy_qty"] == 0.0
    assert result["attribution_pending_qty"] == 2.0


def test_finalized_unmatched_quantity_is_reported_as_cancellation_proxy():
    matcher = TradeDepthMatcher()
    event = matcher.add_depth_event(_depth_event(quantity=5.0))
    matcher.add_trade(_trade(1, price=101.0, offset_ms=1_001))
    result = depth_event_flow_analysis([event], now_ms=event["event_time_ms"])

    assert result["matched_execution_qty"] == 0.0
    assert result["cancellation_proxy_qty"] == 5.0
    assert result["inferred_cancel_qty"] == 5.0


def test_aggressive_buy_matches_ask_reduction():
    matcher = TradeDepthMatcher()
    event = matcher.add_depth_event(_depth_event(side="ask", price=100.5, quantity=2.0))
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
    assert event["matched_ask_execution_qty"] == 2.0
    assert event["matched_trade_ids"] == [1]
    assert event["matched_trade_count"] == 1.0
    assert event["unmatched_reduction_qty"] == 0.0


@pytest.mark.asyncio
async def test_live_scanner_cache_matches_trade_events_to_depth_reductions():
    cache = LiveScannerCache(None, ["BTCUSDT"])
    timestamp = BASE
    event_time_ms = int(timestamp.timestamp() * 1000)
    book = OrderBookSnapshot(
        symbol="BTCUSDT",
        bids=((100.0, 5.0),),
        asks=((100.5, 5.0),),
        last_update_id=11,
        timestamp=timestamp,
        depth_event={
            "event_time_ms": event_time_ms,
            "reduction_qty": 2.0,
            "inferred_cancel_qty": 2.0,
            "reductions": [{"side": "bid", "price": 100.0, "quantity": 2.0}],
        },
    )

    await cache.accept_live_order_book(book)
    await cache.accept_live_trade(_trade(1, quantity=2.0, offset_ms=100))

    state = cache.snapshot("BTCUSDT")
    assert state is not None
    assert state.depth_event_history[0]["matched_execution_qty"] == 2.0



@pytest.mark.asyncio
async def test_depth_producer_to_live_scanner_to_ofi_side_attribution_replay():
    timestamp_ms = int(BASE.timestamp() * 1000)
    local_book = LocalOrderBook("BTCUSDT", max_levels=20)
    initial = OrderBookSnapshot(
        symbol="BTCUSDT",
        bids=((100.0, 5.0),),
        asks=((101.0, 5.0),),
        last_update_id=100,
        timestamp=BASE,
    )
    local_book.seed(initial)
    update = DepthUpdate(
        first_update_id=101,
        final_update_id=101,
        previous_update_id=100,
        bids=((100.0, 3.0),),
        asks=((101.0, 4.0),),
        event_time_ms=timestamp_ms + 100,
    )
    produced = local_book.apply(update)
    assert produced is not None
    assert produced.depth_event is not None
    assert produced.depth_event["bid_reduce_qty"] == 2.0
    assert produced.depth_event["ask_reduce_qty"] == 1.0

    cache = LiveScannerCache(None, ["BTCUSDT"])
    await cache.accept_live_order_book(produced)
    await cache.accept_live_trade(
        _trade(1, side=TradeSide.SELL, price=100.0, quantity=1.0, offset_ms=200)
    )
    await cache.accept_live_trade(
        _trade(2, side=TradeSide.BUY, price=101.0, quantity=0.5, offset_ms=250)
    )
    # Advance the exchange-time watermark beyond the attribution window.
    await cache.accept_live_trade(
        _trade(3, side=TradeSide.BUY, price=102.0, quantity=0.1, offset_ms=800)
    )
    state = cache.snapshot("BTCUSDT")
    assert state is not None
    result = depth_event_flow_analysis(
        list(state.depth_event_history), now_ms=timestamp_ms + 900
    )
    assert result["ofi_available"] is True
    assert result["matched_bid_execution_qty"] == 1.0
    assert result["matched_ask_execution_qty"] == 0.5
    assert result["bid_cancellation_proxy_qty"] == 1.0
    assert result["ask_cancellation_proxy_qty"] == 0.5
    assert result["attribution_pending_qty"] == 0.0
    assert result["execution_attributed_ratio"] == pytest.approx(0.5)
