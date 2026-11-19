from datetime import datetime, timedelta, timezone

from aitos.world_intelligence.execution_replay import (
    ExecutionReplayConfig, FundingObservation, ReplayBookLevel,
    ReplayBookSnapshot, ReplayOrder, replay_execution,
)

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


def test_execution_replay_models_latency_partial_fills_fees_impact_and_funding():
    book = ReplayBookSnapshot(
        timestamp=NOW + timedelta(seconds=1),
        bids=(ReplayBookLevel(99.9, 2),),
        asks=(ReplayBookLevel(100.0, 0.4), ReplayBookLevel(100.1, 0.3)),
    )
    order = ReplayOrder(
        "o1", "buy", 1.0, NOW, position_end_at=NOW + timedelta(seconds=4)
    )
    report = replay_execution(
        [order], [book],
        funding=[FundingObservation(NOW + timedelta(seconds=2), 0.001),
                 FundingObservation(NOW + timedelta(seconds=5), 0.001)],
        config=ExecutionReplayConfig(
            latency=timedelta(milliseconds=500), taker_fee_rate=0.001,
            impact_coefficient_bps=2,
        ),
    )
    assert report.filled_quantity == 0.7
    assert report.unfilled_quantity == 0.3
    assert report.fill_ratio == 0.7
    assert report.fill_count == 2
    assert report.fees_paid > 0
    assert report.estimated_market_impact > 0
    assert report.funding_paid > 0
    assert report.average_fill_price > 100


def test_maker_orders_need_queue_adjusted_fill_evidence_and_short_funding_sign():
    book = ReplayBookSnapshot(
        NOW, (ReplayBookLevel(100, 2),), (ReplayBookLevel(101, 2),),
        maker_bid_fillable_qty=0.5,
    )
    order = ReplayOrder("maker", "buy", 1, NOW, liquidity="maker")
    report = replay_execution(
        [order], [book],
        funding=[FundingObservation(NOW + timedelta(seconds=1), 0.01)],
        config=ExecutionReplayConfig(latency=timedelta(0)),
    )
    assert report.filled_quantity == 0.5
    assert report.unfilled_quantity == 0.5
    assert report.funding_paid > 0

    short = ReplayOrder("short", "sell", 1, NOW)
    short_report = replay_execution(
        [short], [ReplayBookSnapshot(NOW, (ReplayBookLevel(100, 2),), (ReplayBookLevel(101, 2),))],
        funding=[FundingObservation(NOW + timedelta(seconds=1), 0.01)],
        config=ExecutionReplayConfig(latency=timedelta(0)),
    )
    assert short_report.funding_paid < 0
