"""Unit tests for Phase-F offline exit-policy evaluation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aitos.evaluation import (
    ExitReplayEngine,
    PriceBar,
    TradeScenario,
    compare_policies,
)


def _bars_uptrend(n: int = 30, start: float = 79000.0) -> list[PriceBar]:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars: list[PriceBar] = []
    px = start
    for i in range(n):
        o = px
        c = px + 50.0
        bars.append(
            PriceBar(
                timestamp=t0 + timedelta(minutes=i),
                open=o,
                high=c + 10,
                low=o - 10,
                close=c,
                volume=1.0,
            )
        )
        px = c
    return bars


def _bars_reversal(n: int = 30, start: float = 79000.0) -> list[PriceBar]:
    """Rise then sharp drop through SL."""
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars: list[PriceBar] = []
    px = start
    for i in range(n):
        if i < 10:
            drift = 30.0
        else:
            drift = -80.0
        o = px
        c = px + drift
        bars.append(
            PriceBar(
                timestamp=t0 + timedelta(minutes=i),
                open=o,
                high=max(o, c) + 5,
                low=min(o, c) - 5,
                close=c,
                volume=1.0,
            )
        )
        px = c
    return bars


def test_static_hits_tp_on_uptrend():
    scenario = TradeScenario(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=79000.0,
        stop_loss=78500.0,
        take_profit_levels=(79500.0,),
        position_size_usd=10_000.0,
    )
    engine = ExitReplayEngine()
    result = engine.run_static(scenario, _bars_uptrend())
    assert result.exit_reason == "tp_triggered"
    assert result.pnl_usd > 0
    assert result.r_multiple > 0


def test_static_hits_sl_on_reversal():
    scenario = TradeScenario(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=79000.0,
        stop_loss=78700.0,
        take_profit_levels=(81000.0,),
        position_size_usd=10_000.0,
    )
    engine = ExitReplayEngine()
    result = engine.run_static(scenario, _bars_reversal())
    assert result.exit_reason == "sl_triggered"
    assert result.pnl_usd < 0


def test_compare_policies_returns_both():
    scenario = TradeScenario(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=79000.0,
        stop_loss=78500.0,
        take_profit_levels=(80000.0,),
    )
    summary = compare_policies(scenario, _bars_uptrend(40))
    assert summary.static.policy == "static"
    assert summary.eie.policy == "eie"
    assert summary.bars_consumed == 40
    d = summary.to_dict()
    assert "pnl_delta_usd" in d
    assert "eie_better" in d


def test_eie_respects_hard_sl():
    scenario = TradeScenario(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=79000.0,
        stop_loss=78800.0,
        take_profit_levels=(82000.0,),
    )
    engine = ExitReplayEngine()
    result = engine.run_eie(scenario, _bars_reversal())
    assert result.exit_reason == "sl_triggered"
