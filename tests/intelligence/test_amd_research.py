from types import SimpleNamespace

import pytest

from aitos.intelligence.amd_research import (
    AMDDepthSnapshot,
    AMDForwardOutcome,
    AMDHistoricalObservation,
    AMDTradePrint,
    build_event_aligned_order_flow,
    build_live_event_order_flow,
    evaluate_amd_forward_outcome,
    purged_walk_forward_folds,
    replay_amd_execution,
    replay_amd_historical_observations,
    summarize_amd_cohorts,
    summarize_amd_forward_cohorts,
)
from aitos.statistics.replay import BookSnapshot


def _depth(timestamp, bid_size=5.0, ask_size=5.0):
    return AMDDepthSnapshot(
        timestamp_ms=timestamp,
        bids=((100.0, bid_size),),
        asks=((100.2, ask_size),),
    )


def test_event_flow_uses_explicit_aggressor_side_and_deduplicates_trades():
    trades = [
        AMDTradePrint(i * 100, str(i), 100.0, 1.0, "BUY" if i < 9 else "SELL")
        for i in range(10)
    ]
    trades.append(trades[0])
    trades.append(AMDTradePrint(950, "unknown", 100.0, 1.0, None))
    result = build_event_aligned_order_flow(
        trades,
        [_depth(100), _depth(300, 7.0, 4.0), _depth(500, 8.0, 3.0)],
        symbol="BTCUSDT",
        as_of_ms=1000,
        min_trade_events=10,
        min_depth_updates=2,
    )
    assert result.evidence.fresh
    assert result.trade_count == 10
    assert result.duplicate_trade_count == 1
    assert result.unclassified_trade_count == 1
    assert result.evidence.cvd_score > 5.0
    assert result.evidence.bid_replenishment > 0
    assert "unclassified_aggressor_side_excluded" in result.diagnostics


def test_event_flow_fails_closed_for_stale_or_insufficient_inputs():
    result = build_event_aligned_order_flow(
        [AMDTradePrint(100, str(i), 100.0, 1.0, "BUY") for i in range(3)],
        [_depth(100), _depth(200)],
        symbol="BTCUSDT",
        as_of_ms=10_000,
        max_staleness_ms=500,
        min_trade_events=10,
        min_depth_updates=2,
    )
    assert not result.evidence.fresh
    assert result.evidence.confidence == 0.0
    assert "insufficient_classified_trade_events" in result.diagnostics
    assert "event_flow_stale" in result.diagnostics


def test_event_flow_rejects_invalid_depth_snapshot():
    with pytest.raises(ValueError):
        build_event_aligned_order_flow(
            [],
            [AMDDepthSnapshot(1, ((100.0, -1.0),), ((101.0, 1.0),))],
            symbol="BTCUSDT",
            as_of_ms=2,
        )


def test_forward_outcome_reports_mfe_mae_and_incomplete_horizon():
    state = SimpleNamespace(
        symbol="BTCUSDT",
        timeframe="5m",
        state_id="amd-1",
        direction="LONG",
    )
    result = evaluate_amd_forward_outcome(
        state,
        [
            SimpleNamespace(high=102.0, low=99.0, close=101.0),
            {"high": 103.0, "low": 100.0, "close": 102.0},
        ],
        entry_price=100.0,
        entry_time_ms=1000,
        horizon_bars=3,
    )
    assert result.forward_return_pct == pytest.approx(2.0)
    assert result.mfe_pct == pytest.approx(3.0)
    assert result.mae_pct == pytest.approx(-1.0)
    assert result.observed_bars == 2
    assert not result.outcome_complete


def test_forward_outcome_short_excursions_are_directionally_signed():
    state = SimpleNamespace(
        symbol="BTCUSDT",
        timeframe="1m",
        state_id="amd-short",
        direction="SHORT",
    )
    result = evaluate_amd_forward_outcome(
        state,
        [SimpleNamespace(high=102.0, low=97.0, close=98.0)],
        entry_price=100.0,
        entry_time_ms=1000,
        horizon_bars=1,
    )
    assert result.forward_return_pct == pytest.approx(2.0)
    assert result.mfe_pct == pytest.approx(3.0)
    assert result.mae_pct == pytest.approx(-2.0)


def test_walk_forward_folds_are_chronological_and_purged():
    folds = purged_walk_forward_folds(
        list(range(30)),
        min_train_size=10,
        test_size=5,
        step_size=5,
        embargo_bars=2,
    )
    assert folds
    for fold in folds:
        assert max(fold.train_indices) < min(fold.test_indices)
        assert min(fold.test_indices) - max(fold.train_indices) - 1 >= 2
        assert fold.train_end_ms < fold.test_start_ms


def test_walk_forward_rejects_unsorted_timestamps():
    with pytest.raises(ValueError):
        purged_walk_forward_folds([1, 3, 2], min_train_size=2, test_size=1)


def test_cohort_summary_keeps_unresolved_outcomes_separate():
    rows = [
        {
            "variant": "amd",
            "symbol": "BTCUSDT",
            "regime": "trend",
            "phase": "distribution",
            "direction": "LONG",
            "net_return_pct": 2.0,
        },
        {
            "variant": "amd",
            "symbol": "BTCUSDT",
            "regime": "trend",
            "phase": "distribution",
            "direction": "LONG",
            "net_return_pct": -1.0,
        },
        {
            "variant": "amd",
            "symbol": "BTCUSDT",
            "regime": "trend",
            "phase": "distribution",
            "direction": "LONG",
            "net_return_pct": None,
        },
    ]
    summary = summarize_amd_cohorts(rows)[0]
    assert summary["observations"] == 3
    assert summary["resolved_outcomes"] == 2
    assert summary["unresolved_outcomes"] == 1
    assert summary["win_rate"] == pytest.approx(0.5)
    assert summary["profit_factor"] == pytest.approx(2.0)


def test_execution_replay_accounts_for_both_legs_and_all_costs():
    entry = [
        BookSnapshot(100, 100.0, 100.2, 2.0, 2.0),
        BookSnapshot(200, 100.0, 100.2, 2.0, 2.0),
    ]
    exit_books = [
        BookSnapshot(1000, 100.8, 101.0, 2.0, 2.0),
        BookSnapshot(1100, 100.8, 101.0, 2.0, 2.0),
    ]
    result = replay_amd_execution(
        entry,
        exit_books,
        direction="LONG",
        quantity=1.0,
        entry_submitted_at_ms=100,
        exit_submitted_at_ms=1000,
        latency_ms=0,
        taker_fee_bps=4.0,
        slippage_bps=2.0,
        funding_bps=1.0,
        holding_hours=8.0,
    )
    assert result.complete
    assert result.entry.complete and result.exit.complete
    assert result.entry.fees > 0 and result.exit.fees > 0
    assert result.entry.funding_cost > 0
    assert result.total_cost > result.entry.total_cost
    assert result.net_pnl is not None
    assert result.net_return_pct is not None


def test_execution_replay_does_not_fabricate_exit_when_entry_unfilled():
    result = replay_amd_execution(
        [],
        [BookSnapshot(1000, 100.8, 101.0, 2.0, 2.0)],
        direction="LONG",
        quantity=1.0,
        entry_submitted_at_ms=100,
        exit_submitted_at_ms=1000,
        latency_ms=0,
        taker_fee_bps=4.0,
    )
    assert not result.complete
    assert result.entry.filled_quantity == 0.0
    assert result.exit.filled_quantity == 0.0
    assert result.net_pnl is None


def test_live_event_flow_uses_aitos_trade_ticks_and_sequenced_depth_events():
    from datetime import datetime, timezone

    trades = [
        SimpleNamespace(
            timestamp=datetime.fromtimestamp(index / 10, tz=timezone.utc),
            trade_id=index,
            price=100.0,
            quantity=1.0,
            side="BUY" if index < 9 else "SELL",
        )
        for index in range(10)
    ]
    events = [
        {
            "event_time_ms": 900,
            "first_update_id": 10,
            "final_update_id": 10,
            "previous_update_id": 9,
            "ofi_raw": 2.0,
            "ofi_scale": 4.0,
            "bid_replenishment_qty": 1.0,
            "ask_replenishment_qty": 0.0,
        },
        {
            "event_time_ms": 950,
            "first_update_id": 11,
            "final_update_id": 11,
            "previous_update_id": 10,
            "ofi_raw": 1.0,
            "ofi_scale": 2.0,
            "bid_replenishment_qty": 0.5,
            "ask_replenishment_qty": 0.0,
        },
    ]
    result = build_live_event_order_flow(
        trades,
        events,
        as_of_ms=1000,
        min_trade_events=10,
        min_depth_events=2,
    )
    assert result.evidence.fresh
    assert result.evidence.cvd_score > 5.0
    assert result.evidence.ofi_score > 5.0
    assert result.evidence.bid_replenishment == pytest.approx(1.5)


def test_live_event_flow_rejects_depth_sequence_gaps():
    from datetime import datetime, timezone

    trades = [
        SimpleNamespace(
            timestamp=datetime.fromtimestamp(index / 10, tz=timezone.utc),
            trade_id=index,
            price=100.0,
            quantity=1.0,
            side="BUY",
        )
        for index in range(10)
    ]
    events = [
        {
            "event_time_ms": 900,
            "final_update_id": 10,
            "previous_update_id": 9,
            "ofi_raw": 1.0,
            "ofi_scale": 2.0,
        },
        {
            "event_time_ms": 950,
            "final_update_id": 12,
            "previous_update_id": 11,
            "ofi_raw": 1.0,
            "ofi_scale": 2.0,
        },
    ]
    result = build_live_event_order_flow(
        trades,
        events,
        as_of_ms=1000,
        min_trade_events=10,
        min_depth_events=2,
    )
    assert not result.evidence.fresh
    assert "depth_sequence_gap" in result.diagnostics
    assert result.evidence.confidence == 0.0


def test_live_event_flow_fails_closed_when_depth_history_was_reset_inside_window():
    from datetime import datetime, timezone

    trades = [
        SimpleNamespace(
            timestamp=datetime.fromtimestamp(index / 10, tz=timezone.utc),
            trade_id=index,
            price=100.0,
            quantity=1.0,
            side="BUY",
        )
        for index in range(10)
    ]
    events = [
        {
            "event_time_ms": 900,
            "final_update_id": 10,
            "previous_update_id": 9,
            "ofi_raw": 1.0,
            "ofi_scale": 2.0,
        },
        {
            "event_time_ms": 950,
            "final_update_id": 11,
            "previous_update_id": 10,
            "ofi_raw": 1.0,
            "ofi_scale": 2.0,
        },
    ]
    result = build_live_event_order_flow(
        trades,
        events,
        as_of_ms=1000,
        min_trade_events=10,
        min_depth_events=2,
        history_gap_at_ms=925,
    )
    assert not result.evidence.fresh
    assert "depth_history_gap_in_window" in result.diagnostics


def test_execution_replay_reports_partial_exit_as_incomplete_and_matches_fills():
    entry = [BookSnapshot(100, 100.0, 100.2, 2.0, 1.0)]
    exit_books = [BookSnapshot(1000, 101.0, 101.2, 0.4, 2.0)]
    result = replay_amd_execution(
        entry,
        exit_books,
        direction="LONG",
        quantity=1.0,
        entry_submitted_at_ms=100,
        exit_submitted_at_ms=1000,
        latency_ms=0,
        taker_fee_bps=4.0,
    )
    assert not result.complete
    assert result.entry.filled_quantity == pytest.approx(1.0)
    assert result.exit.filled_quantity == pytest.approx(0.4)
    assert result.gross_pnl == pytest.approx(0.32)
    assert result.net_pnl is not None


def test_forward_outcome_rejects_candle_at_or_before_amd_event_time():
    state = SimpleNamespace(
        symbol="BTCUSDT",
        timeframe="5m",
        state_id="amd-lookahead",
        direction="LONG",
    )
    with pytest.raises(ValueError, match="must occur after"):
        evaluate_amd_forward_outcome(
            state,
            [
                {
                    "timestamp_ms": 1000,
                    "high": 102.0,
                    "low": 99.0,
                    "close": 101.0,
                }
            ],
            entry_price=100.0,
            entry_time_ms=1000,
            horizon_bars=1,
        )


def test_historical_amd_replay_uses_closed_candles_and_archived_event_time():
    from datetime import datetime, timedelta, timezone

    from aitos.models.market import OrderBookSnapshot, TradeSide, TradeTick

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    events = []
    update_id = 100
    for index in range(25):
        open_time = base + timedelta(minutes=5 * index)
        close_time = open_time + timedelta(minutes=5)
        candles.append(
            {
                "timeframe": "5m",
                "open_time": open_time,
                "close_time": close_time,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 10.0,
            }
        )
        for trade_index in range(10):
            trade_time = close_time - timedelta(seconds=10 - trade_index)
            events.append(
                TradeTick(
                    symbol="BTCUSDT",
                    trade_id=index * 10 + trade_index,
                    price=100.0,
                    quantity=1.0,
                    side=TradeSide.BUY,
                    is_buyer_maker=False,
                    timestamp=trade_time,
                )
            )
        for depth_index in range(2):
            event_time = close_time - timedelta(seconds=2 - depth_index)
            events.append(
                OrderBookSnapshot(
                    symbol="BTCUSDT",
                    bids=((100.0, 5.0),),
                    asks=((100.2, 5.0),),
                    last_update_id=update_id,
                    timestamp=event_time,
                    depth_event={
                        "event_time_ms": int(event_time.timestamp() * 1000),
                        "first_update_id": update_id,
                        "final_update_id": update_id,
                        "previous_update_id": update_id - 1,
                        "ofi_raw": 1.0,
                        "ofi_scale": 2.0,
                        "bid_replenishment_qty": 0.5,
                        "ask_replenishment_qty": 0.0,
                        "reductions": [],
                    },
                )
            )
            update_id += 1
    dataset = SimpleNamespace(
        symbol="BTCUSDT",
        candles=tuple(candles),
        events=tuple(sorted(events, key=lambda item: item.timestamp)),
        provenance={"raw_depth_event_replay_available": True},
    )
    observations = replay_amd_historical_observations(
        dataset,
        timeframe="5m",
        horizon_bars=2,
    )
    assert len(observations) == 6
    assert observations[0].flow_source == "sequenced_trade_and_depth_events"
    assert observations[-1].state.as_of == close_time.isoformat()
    assert all(
        item.decision_time_ms <= int(close_time.timestamp() * 1000)
        for item in observations
    )


def test_historical_amd_replay_rejects_snapshot_only_datasets():
    dataset = SimpleNamespace(
        symbol="BTCUSDT",
        candles=(),
        events=(),
        provenance={"raw_depth_event_replay_available": False},
    )
    with pytest.raises(ValueError, match="raw sequenced depth"):
        replay_amd_historical_observations(dataset, timeframe="5m")


def test_forward_cohort_summary_does_not_call_gross_markout_net_pnl():
    state = SimpleNamespace(phase=SimpleNamespace(value="distribution_confirmed"))
    outcomes = [
        AMDForwardOutcome(
            symbol="BTCUSDT",
            timeframe="5m",
            state_id="amd-1",
            direction="LONG",
            entry_time_ms=1000,
            entry_price=100.0,
            horizon_bars=2,
            observed_bars=2,
            forward_return_pct=1.0,
            mfe_pct=2.0,
            mae_pct=-0.5,
            outcome_complete=True,
        ),
        AMDForwardOutcome(
            symbol="BTCUSDT",
            timeframe="5m",
            state_id="amd-2",
            direction="LONG",
            entry_time_ms=2000,
            entry_price=100.0,
            horizon_bars=2,
            observed_bars=1,
            forward_return_pct=-0.5,
            mfe_pct=1.0,
            mae_pct=-1.0,
            outcome_complete=False,
        ),
    ]
    observations = [
        AMDHistoricalObservation(
            state=state,
            outcome=outcome,
            decision_time_ms=outcome.entry_time_ms,
            flow_diagnostics=(),
            flow_source="sequenced_trade_and_depth_events",
        )
        for outcome in outcomes
    ]
    summary = summarize_amd_forward_cohorts(observations)[0]
    assert summary["observations"] == 2
    assert summary["complete_horizons"] == 1
    assert summary["incomplete_horizons"] == 1
    assert summary["mean_forward_return_pct"] == pytest.approx(1.0)
    assert summary["outcome_type"] == "counterfactual_gross_candle_markout"
    assert "net_return_pct" not in summary
