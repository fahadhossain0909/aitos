from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aitos.backtest.capital_rebalance import (
    CapitalRebalanceReplay,
    CapitalRebalanceReplayFrame,
)
from aitos.intelligence.capital_rebalancing import (
    IncumbentPosition,
    RebalanceCandidate,
    RebalanceContext,
)


def _frame(timestamp, incumbent_symbol, candidate_symbol, candidate_confidence, returns):
    incumbent = IncumbentPosition(
        symbol=incumbent_symbol,
        side="LONG",
        remaining_net_edge_pct=0.05,
        hold_seconds=600,
        stagnation_score=0.9,
        thesis_health="degraded",
        exit_fee_bps=1.0,
        exit_slippage_bps=1.0,
    )
    candidate = RebalanceCandidate(
        symbol=candidate_symbol,
        side="LONG",
        expected_gross_return_pct=2.0,
        expected_loss_pct=0.5,
        loss_probability=0.1,
        fee_bps=4.0,
        slippage_bps=2.0,
        funding_bps=0.0,
        confidence=candidate_confidence,
        liquidity_score=8.0,
        risk_pct=0.5,
        liquidation_distance_pct=5.0,
    )
    context = RebalanceContext(
        portfolio_risk_after_incumbent_close_pct=1.0,
        candidate_correlated_incremental_risk_pct=0.5,
        drawdown_pct=1.0,
        available_capital_after_incumbent_close_usd=1000.0,
        candidate_capital_usd=500.0,
    )
    return CapitalRebalanceReplayFrame(
        timestamp=timestamp,
        incumbent=incumbent,
        candidate=candidate,
        context=context,
        realized_returns_pct=returns,
    )


def test_paired_replay_compares_switch_policy_with_hold_only_without_lookahead():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    frames = [
        _frame(
            start,
            "BTCUSDT",
            "ETHUSDT",
            0.95,
            {"BTCUSDT": 1.0, "ETHUSDT": 2.0},
        ),
        _frame(
            start + timedelta(minutes=15),
            "ETHUSDT",
            "SOLUSDT",
            0.50,
            {"BTCUSDT": -2.0, "ETHUSDT": 3.0, "SOLUSDT": 4.0},
        ),
    ]

    result = CapitalRebalanceReplay().run(
        frames,
        initial_equity=1000.0,
        final_exit_fee_bps=1.0,
        final_exit_slippage_bps=1.0,
    )

    assert result.hold_only.switches == 0
    assert result.rebalanced.switches == 1
    assert result.switch_log[0][1:3] == ("BTCUSDT", "ETHUSDT")
    assert result.rebalanced.final_equity > result.hold_only.final_equity
    assert result.incremental_equity == pytest.approx(
        result.rebalanced.final_equity - result.hold_only.final_equity
    )


def test_paired_replay_rejects_out_of_order_frames():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    frames = [
        _frame(
            start + timedelta(minutes=15),
            "BTCUSDT",
            "ETHUSDT",
            0.95,
            {"BTCUSDT": 0.1, "ETHUSDT": 0.2},
        ),
        _frame(
            start,
            "BTCUSDT",
            "SOLUSDT",
            0.95,
            {"BTCUSDT": 0.1, "SOLUSDT": 0.2},
        ),
    ]
    with pytest.raises(ValueError, match="timestamp ordered"):
        CapitalRebalanceReplay().run(frames, initial_equity=1000.0)


def test_paired_replay_requires_point_in_time_active_incumbent_evidence():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    frames = [
        _frame(
            start,
            "BTCUSDT",
            "ETHUSDT",
            0.95,
            {"BTCUSDT": 0.1, "ETHUSDT": 0.2},
        ),
        _frame(
            start + timedelta(minutes=15),
            "BTCUSDT",
            "SOLUSDT",
            0.95,
            {"BTCUSDT": 0.1, "ETHUSDT": 0.2, "SOLUSDT": 0.3},
        ),
    ]
    with pytest.raises(ValueError, match="incumbent must match"):
        CapitalRebalanceReplay().run(frames, initial_equity=1000.0)
