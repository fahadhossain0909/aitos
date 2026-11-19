import pytest

from aitos.statistics.depth_replay import (
    DepthBookSnapshot,
    LimitQueueEvent,
    replay_l2_market_order,
    replay_passive_limit_queue,
)


def test_l2_market_order_walks_multiple_levels_with_latency_fees_and_impact():
    book = DepthBookSnapshot(
        1200,
        bids=((99.9, 3.0), (99.8, 4.0)),
        asks=((100.1, 0.5), (100.2, 0.75), (100.4, 2.0)),
    )
    result = replay_l2_market_order(
        [book],
        side="BUY",
        quantity=1.0,
        submitted_at_ms=1000,
        latency_ms=100,
        fee_bps=5.0,
        impact_coefficient_bps=8.0,
        max_participation=0.5,
        slippage_bps=2.0,
        funding_bps=1.0,
        borrow_bps=0.5,
        holding_hours=4.0,
    )
    assert result.complete
    assert result.filled_quantity == pytest.approx(1.0)
    assert len(result.fills) == 2
    assert result.average_fill_price > 100.1
    assert result.fees > 0.0
    assert result.impact_cost > 0.0
    assert result.spread_cost > 0.0
    assert result.slippage_cost > 0.0
    assert result.funding_cost > 0.0
    assert result.borrow_cost > 0.0
    assert result.total_cost == pytest.approx(
        result.fees
        + result.spread_cost
        + result.impact_cost
        + result.slippage_cost
        + result.funding_cost
        + result.borrow_cost
    )


def test_l2_market_order_keeps_unfilled_remainder_when_depth_is_insufficient():
    result = replay_l2_market_order(
        [DepthBookSnapshot(1000, ((99.0, 0.1),), ((101.0, 0.2),))],
        side="BUY",
        quantity=1.0,
        submitted_at_ms=1000,
        latency_ms=0,
        fee_bps=0.0,
        max_participation=1.0,
    )
    assert result.filled_quantity == pytest.approx(0.2)
    assert result.remaining_quantity == pytest.approx(0.8)
    assert not result.complete


def test_passive_queue_replay_consumes_ahead_queue_before_own_fill():
    result = replay_passive_limit_queue(
        [
            LimitQueueEvent(1000, traded_at_limit=4.0),
            LimitQueueEvent(1100, cancelled_ahead=2.0, traded_at_limit=3.0),
        ],
        quantity=2.0,
        queue_ahead=5.0,
        submitted_at_ms=900,
    )
    assert result.filled_quantity == pytest.approx(2.0)
    assert result.complete
    assert result.fills == ((1100, 2.0),)



def test_l2_market_order_does_not_reuse_later_snapshot_depth():
    books = [
        DepthBookSnapshot(1000, ((99.0, 1.0),), ((101.0, 0.2),)),
        DepthBookSnapshot(1100, ((100.0, 1.0),), ((100.5, 1.0),)),
    ]
    result = replay_l2_market_order(
        books,
        side="BUY",
        quantity=0.5,
        submitted_at_ms=1000,
        latency_ms=0,
        fee_bps=0.0,
    )
    assert result.filled_quantity == pytest.approx(0.2)
    assert result.remaining_quantity == pytest.approx(0.3)
    assert not result.complete



def test_l2_market_order_accepts_signed_funding_credit():
    book = DepthBookSnapshot(
        1000,
        bids=((99.0, 2.0),),
        asks=((101.0, 2.0),),
    )
    result = replay_l2_market_order(
        [book],
        side="BUY",
        quantity=1.0,
        submitted_at_ms=1000,
        latency_ms=0,
        fee_bps=0.0,
        funding_bps=-8.0,
        holding_hours=8.0,
        funding_interval_hours=8.0,
    )
    assert result.funding_cost < 0
    assert result.total_cost == pytest.approx(
        result.fees
        + result.spread_cost
        + result.impact_cost
        + result.slippage_cost
        + result.funding_cost
        + result.borrow_cost
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"quantity": True},
        {"fee_bps": True},
        {"impact_coefficient_bps": True},
        {"max_participation": True},
        {"slippage_bps": True},
        {"borrow_bps": True},
        {"holding_hours": True},
        {"submitted_at_ms": 1000.5},
        {"latency_ms": 0.5},
    ],
)
def test_l2_market_order_rejects_boolean_and_non_integer_time_inputs(overrides):
    arguments = {
        "side": "BUY",
        "quantity": 1.0,
        "submitted_at_ms": 1000,
        "latency_ms": 0,
        "fee_bps": 0.0,
    }
    arguments.update(overrides)
    with pytest.raises(ValueError):
        replay_l2_market_order([], **arguments)


@pytest.mark.parametrize(
    "values",
    [
        {"timestamp_ms": True},
        {"timestamp_ms": 1000, "traded_at_limit": True},
        {"timestamp_ms": 1000, "cancelled_ahead": -1.0},
    ],
)
def test_queue_event_rejects_invalid_quantities(values):
    with pytest.raises(ValueError):
        LimitQueueEvent(**values)


def test_queue_replay_rejects_boolean_and_non_integer_inputs():
    with pytest.raises(ValueError):
        replay_passive_limit_queue(
            [],
            quantity=True,
            queue_ahead=0.0,
            submitted_at_ms=1000,
        )
    with pytest.raises(ValueError):
        replay_passive_limit_queue(
            [],
            quantity=1.0,
            queue_ahead=0.0,
            submitted_at_ms=1000.5,
        )
    with pytest.raises(TypeError, match="LimitQueueEvent"):
        replay_passive_limit_queue(
            [object()],
            quantity=1.0,
            queue_ahead=0.0,
            submitted_at_ms=1000,
        )
