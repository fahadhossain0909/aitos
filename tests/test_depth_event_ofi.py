from dataclasses import replace
from datetime import datetime, timezone

from aitos.exchange.orderbook import DepthUpdate, LocalOrderBook
from aitos.intelligence.liquidity import depth_event_flow_analysis
from aitos.market_data.legacy_bridge import book_snapshot_event
from aitos.models.market import OrderBookSnapshot


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
    assert snapshot.depth_event["reductions"] == [
        {"side": "ask", "price": 101.0, "quantity": 2.0}
    ]


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
    event = {
        "ofi_raw": 1.25,
        "ofi_scale": 50.0,
        "ofi_normalized": 0.025,
        "reductions": [
            {
                "side": "bid",
                "price": 100.0,
                "quantity": 2.0,
                "matched_execution_qty": 1.0,
            }
        ],
    }
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


def test_ofi_confidence_requires_minimum_fresh_timestamped_depth_events():
    now_ms = 2_000_000
    events = [
        {
            "event_time_ms": now_ms - age_ms,
            "ofi_raw": 2.0,
            "ofi_scale": 100.0,
            "bid_add_qty": 2.0,
        }
        for age_ms in (100, 200, 300)
    ]

    result = depth_event_flow_analysis(events, now_ms=now_ms)

    assert result["ofi_available"] is True
    assert result["fresh_event_count"] == 3.0
    assert result["stale_event_count"] == 0.0
    assert 0.0 < result["ofi_confidence"] <= 1.0
    assert result["freshness_ms"] == 100.0


def test_stale_depth_events_are_excluded_from_ofi_score():
    now_ms = 2_000_000
    events = [
        {
            "event_time_ms": now_ms - 5_000,
            "ofi_raw": 100.0,
            "ofi_scale": 100.0,
            "bid_add_qty": 100.0,
        }
        for _ in range(5)
    ]

    result = depth_event_flow_analysis(events, now_ms=now_ms)

    assert result["ofi_available"] is False
    assert result["ofi_confidence"] == 0.0
    assert result["fresh_event_count"] == 0.0
    assert result["stale_event_count"] == 5.0
    assert result["ofi_score"] == 5.0


def test_timestamped_ofi_aggregates_only_fresh_events():
    now_ms = 2_000_000
    events = [
        {
            "event_time_ms": now_ms - 5_000,
            "ofi_raw": 100.0,
            "ofi_scale": 100.0,
        },
        *[
            {
                "event_time_ms": now_ms - age_ms,
                "ofi_raw": -2.0,
                "ofi_scale": 100.0,
            }
            for age_ms in (100, 200, 300)
        ],
    ]

    result = depth_event_flow_analysis(events, now_ms=now_ms)

    assert result["ofi_available"] is True
    assert result["ofi_score"] < 5.0
    assert result["fresh_event_count"] == 3.0
    assert result["stale_event_count"] == 1.0


def test_bid_and_ask_cancel_pressure_are_attributed_independently():
    now_ms = 2_000_000
    events = [
        {
            "event_time_ms": now_ms - age_ms,
            "ofi_raw": 0.0,
            "ofi_scale": 100.0,
            "bid_cancellation_proxy_qty": 3.0,
            "matched_bid_execution_qty": 1.0,
            "ask_cancellation_proxy_qty": 1.0,
            "matched_ask_execution_qty": 3.0,
        }
        for age_ms in (100, 200, 300)
    ]
    result = depth_event_flow_analysis(events, now_ms=now_ms)
    assert result["ofi_available"] is True
    assert result["bid_inferred_cancel_pressure"] == 0.75
    assert result["ask_inferred_cancel_pressure"] == 0.25


def test_no_side_cancel_pressure_is_zero_when_no_reductions_exist():
    result = depth_event_flow_analysis(
        [
            {"event_time_ms": 1_999_900, "ofi_raw": 0.0, "ofi_scale": 100.0}
            for _ in range(3)
        ],
        now_ms=2_000_000,
    )
    assert result["bid_inferred_cancel_pressure"] == 0.0
    assert result["ask_inferred_cancel_pressure"] == 0.0


def test_legacy_depth_events_keep_diagnostic_score_but_are_not_live_available():
    result = depth_event_flow_analysis([{"ofi_raw": 2.0, "ofi_scale": 100.0}])

    assert result["ofi_score"] == 6.0
    assert result["ofi_available"] is False
    assert result["ofi_confidence"] == 0.0
    assert result["freshness_ms"] == -1.0
