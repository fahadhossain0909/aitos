from datetime import datetime, timedelta, timezone
import json

import pytest

from aitos.backtest.clickhouse_replay import ClickHouseReplayLoader
from aitos.backtest.aitos_runner import AITOSHistoricalRunner, HistoricalDecision
from aitos.models.market import OrderBookSnapshot, TradeTick


class _Repository:
    async def _query(self, sql, parameters):
        if "FROM trade_ticks" in sql:
            return [
                {
                    "time": parameters["start"] + timedelta(seconds=2),
                    "symbol": "BTCUSDT",
                    "trade_id": 10,
                    "price": 100.0,
                    "quantity": 0.2,
                    "side": "buy",
                    "is_buyer_maker": False,
                }
            ]
        if "FROM order_book_snapshots" in sql:
            return [
                {
                    "time": parameters["start"] + timedelta(seconds=1),
                    "symbol": "BTCUSDT",
                    "bid_levels": json.dumps([[99.0, 2.0]]),
                    "ask_levels": json.dumps([[101.0, 3.0]]),
                    "last_update_id": 55,
                }
            ]
        if "FROM depth_events" in sql:
            return [
                {
                    "event_time": parameters["start"] + timedelta(seconds=1),
                    "final_update_id": 55,
                    "payload_json": json.dumps(
                        {"event_time_ms": 1_000, "ofi_raw": 2.0}
                    ),
                }
            ]
        if "FROM funding_rates" in sql:
            return [
                {
                    "time": parameters["start"] + timedelta(hours=8),
                    "funding_rate": 0.0001,
                    "mark_price": 100.0,
                }
            ]
        raise AssertionError("unexpected replay query")


@pytest.mark.asyncio
async def test_clickhouse_loader_builds_bounded_point_in_time_dataset():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    dataset = await ClickHouseReplayLoader(_Repository()).load(
        "btcusdt", start, end, limit=100
    )
    assert len(dataset.events) == 2
    assert isinstance(dataset.events[0], OrderBookSnapshot)
    assert dataset.events[0].depth_event["ofi_raw"] == 2.0
    assert isinstance(dataset.events[1], TradeTick)
    assert dataset.funding_events[0]["funding_rate"] == pytest.approx(0.0001)
    assert dataset.provenance["depth_event_history_available"] is True
    assert (
        dataset.provenance["lookahead_policy"] == "strict [start,end), no forward fill"
    )


@pytest.mark.asyncio
async def test_clickhouse_loader_rejects_naive_or_invalid_time_ranges():
    start = datetime(2026, 1, 1)
    with pytest.raises(ValueError, match="timezone-aware"):
        await ClickHouseReplayLoader(_Repository()).load(
            "BTCUSDT", start, start + timedelta(days=1)
        )


def test_historical_runner_applies_funding_only_at_scheduled_settlement():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        OrderBookSnapshot(
            symbol="BTCUSDT",
            bids=((100.0, 5.0),),
            asks=((101.0, 5.0),),
            last_update_id=1,
            timestamp=start,
        ),
        OrderBookSnapshot(
            symbol="BTCUSDT",
            bids=((100.0, 5.0),),
            asks=((101.0, 5.0),),
            last_update_id=2,
            timestamp=start + timedelta(hours=9),
        ),
    ]
    runner = AITOSHistoricalRunner(
        symbol="BTCUSDT",
        tick_size=0.1,
        initial_cash=1_000.0,
        leverage=2.0,
    )
    decisions = 0

    def decide(_state):
        nonlocal decisions
        decisions += 1
        if decisions == 1:
            return HistoricalDecision("long", 1.0, 1.0)
        return HistoricalDecision("short", 1.0, 1.0)

    result = runner.run(
        events,
        decide,
        funding_events=[
            {
                "timestamp": start + timedelta(hours=8),
                "funding_rate": 0.0001,
                "mark_price": 100.0,
            }
        ],
    )
    assert result.funding_events_applied == 1
    assert result.funding_events_skipped_without_mark == 0
    assert result.funding_paid == pytest.approx(0.01)



class _RawDepthRepository:
    async def _query(self, sql, parameters):
        start = parameters["start"]
        if "FROM order_book_snapshots" in sql and "time <" in sql:
            return [
                {
                    "time": start - timedelta(seconds=1),
                    "symbol": "BTCUSDT",
                    "bid_levels": json.dumps([[100.0, 5.0]]),
                    "ask_levels": json.dumps([[101.0, 5.0]]),
                    "last_update_id": 100,
                }
            ]
        if "FROM trade_ticks" in sql or (
            "FROM order_book_snapshots" in sql and "time >=" in sql
        ):
            return []
        if "FROM depth_events" in sql:
            return [
                {
                    "event_time": start + timedelta(seconds=1),
                    "final_update_id": 101,
                    "payload_json": json.dumps(
                        {
                            "first_update_id": 101,
                            "final_update_id": 101,
                            "previous_update_id": 100,
                            "bids": [[100.0, 3.0]],
                            "asks": [[101.0, 4.0]],
                        }
                    ),
                }
            ]
        if "FROM funding_rates" in sql:
            return []
        raise AssertionError("unexpected replay query")


@pytest.mark.asyncio
async def test_clickhouse_loader_replays_raw_depth_deltas_from_pre_window_bootstrap():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    dataset = await ClickHouseReplayLoader(_RawDepthRepository()).load(
        "BTCUSDT", start, start + timedelta(days=1)
    )
    depth_events = [
        event.depth_event
        for event in dataset.events
        if isinstance(event, OrderBookSnapshot) and event.depth_event
    ]
    assert len(depth_events) == 1
    assert depth_events[0]["bid_reduce_qty"] == pytest.approx(2.0)
    assert depth_events[0]["ask_reduce_qty"] == pytest.approx(1.0)
    assert dataset.provenance["raw_depth_events_replayed"] == 1
    assert dataset.provenance["raw_depth_event_replay_available"] is True
    assert dataset.provenance["depth_event_history_available"] is True
