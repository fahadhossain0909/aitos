from datetime import datetime, timezone
from dataclasses import replace

from aitos.exchange.orderbook import DepthUpdate, LocalOrderBook
from aitos.intelligence.liquidity import depth_event_flow_analysis
from aitos.models.market import OrderBookSnapshot
from aitos.market_data.legacy_bridge import book_snapshot_event


def _seed_book():
    return OrderBookSnapshot(
        symbol="BTCUSDT",
        bids=((100.0, 10.0), (99.5, 8.0)),
        asks=((100.5, 9.0), (101.0, 7.0)),
        last_update_id=10,
        timestamp=datetime.now(timezone.utc),
    )


def test_full_depth_event_ofi_uses_non_top_price_levels():
    book = LocalOrderBook("BTCUSDT", max_levels=20)
    book.seed(_seed_book())
    snapshot = book.apply(
        DepthUpdate(
            first_update_id=11,
            final_update_id=11,
            previous_update_id=10,
            bids=((99.5, 12.0),),
            asks=((101.0, 5.0),),
            event_time_ms=1_000_000,
        )
    )
    assert snapshot is not None
    assert snapshot.depth_event is not None
    assert snapshot.depth_event["bid_add_qty"] == 4.0
    assert snapshot.depth_event["ask_reduce_qty"] == 2.0
    assert snapshot.depth_event["ofi_raw"] > 0


def test_same_price_liquidity_reappearing_is_counted_as_replenishment():
    book = LocalOrderBook("BTCUSDT", max_levels=20)
    book.seed(_seed_book())
    first = book.apply(DepthUpdate(11, 11, 10, ((100.0, 6.0),), (), 1_000_000))
    assert first is not None
    second = book.apply(DepthUpdate(12, 12, 11, ((100.0, 9.0),), (), 1_001_000))
    assert second is not None
    assert second.depth_event is not None
    assert second.depth_event["replenishment_qty"] == 3.0


def test_depth_event_aggregation_is_depth_weighted_and_bounded():
    result = depth_event_flow_analysis(
        [
            {
                "ofi_raw": 2.0,
                "ofi_scale": 100.0,
                "bid_add_qty": 2.0,
                "bid_reduce_qty": 0.0,
                "ask_add_qty": 0.0,
                "ask_reduce_qty": 0.0,
                "inferred_cancel_qty": 0.0,
                "replenishment_qty": 0.0,
                "addition_qty": 2.0,
            },
            {
                "ofi_raw": 0.0,
                "ofi_scale": 100.0,
                "bid_add_qty": 0.0,
                "bid_reduce_qty": 1.0,
                "ask_add_qty": 0.0,
                "ask_reduce_qty": 0.0,
                "inferred_cancel_qty": 1.0,
                "replenishment_qty": 0.5,
                "addition_qty": 2.0,
            },
        ]
    )
    assert result["ofi_score"] == 5.5
    assert result["inferred_cancel_qty"] == 1.0
    assert result["replenishment_qty"] == 0.5
    assert result["replenishment_ratio"] == 0.5
    assert result["inferred_cancel_pressure"] == 0.2


def test_order_book_depth_event_round_trips_through_wire_model():
    book = _seed_book()
    event = {"ofi_raw": 1.25, "ofi_scale": 50.0, "ofi_normalized": 0.025}
    restored = OrderBookSnapshot.from_dict({**book.to_dict(), "depth_event": event})
    assert restored.depth_event == event


def test_depth_event_metrics_survive_canonical_market_data_bridge():
    event_metrics = {
        "ofi_raw": 2.0,
        "ofi_scale": 100.0,
        "ofi_normalized": 0.02,
    }
    event = book_snapshot_event(replace(_seed_book(), depth_event=event_metrics))
    assert event.payload["depth_event"] == event_metrics
