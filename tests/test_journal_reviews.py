from datetime import datetime, timezone

from aitos.journal.reviews import (daily_review, monthly_review, r_multiple,
                                   weekly_review)
from aitos.models.trade import Trade, TradeLifecycleState, TradeSide

NOW = datetime.now(timezone.utc).isoformat()


def make_closed_trade(pnl, risk_amount_usd=100.0, strategy_id="strat-a", **overrides):
    defaults = dict(
        trade_id=f"trade-{pnl}-{strategy_id}",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        quantity=1.0,
        leverage=5.0,
        position_size_usd=1000.0,
        risk_amount_usd=risk_amount_usd,
        strategy_id=strategy_id,
        agent_consensus={},
        explanation="",
        sl_price=98.0,
        tp_price=104.0,
        state=TradeLifecycleState.POSITION_CLOSED,
        entry_time=NOW,
        pnl=pnl,
        pnl_percent=(pnl / 1000.0 * 100) if pnl is not None else None,
    )
    defaults.update(overrides)
    return Trade(**defaults)


def test_r_multiple_computation():
    trade = make_closed_trade(pnl=250.0, risk_amount_usd=100.0)
    assert r_multiple(trade) == 2.5


def test_r_multiple_zero_when_no_risk_amount():
    trade = make_closed_trade(pnl=100.0, risk_amount_usd=0.0)
    assert r_multiple(trade) == 0.0


def test_daily_review_empty_trades():
    review = daily_review([], date="2026-07-10")
    assert review.total_trades == 0
    assert review.win_rate == 0.0


def test_daily_review_computes_stats():
    trades = [
        make_closed_trade(pnl=100.0),
        make_closed_trade(pnl=-50.0),
        make_closed_trade(pnl=200.0),
    ]
    review = daily_review(trades, date="2026-07-10")
    assert review.total_trades == 3
    assert review.wins == 2
    assert review.losses == 1
    assert review.win_rate == round(2 / 3 * 100, 2)
    assert review.total_pnl == 250.0
    assert review.best_trade_pnl == 200.0
    assert review.worst_trade_pnl == -50.0


def test_daily_review_ignores_open_trades():
    open_trade = make_closed_trade(pnl=None, state=TradeLifecycleState.POSITION_OPENED)
    closed_trade = make_closed_trade(pnl=100.0)
    review = daily_review([open_trade, closed_trade], date="2026-07-10")
    assert review.total_trades == 1


def test_weekly_review_groups_by_strategy():
    trades = [
        make_closed_trade(pnl=100.0, strategy_id="strat-a"),
        make_closed_trade(pnl=-30.0, strategy_id="strat-a"),
        make_closed_trade(pnl=50.0, strategy_id="strat-b"),
    ]
    review = weekly_review(trades, week_start="2026-07-06")
    assert review.total_trades == 3
    assert review.by_strategy["strat-a"]["trades"] == 2
    assert review.by_strategy["strat-a"]["pnl"] == 70.0
    assert review.by_strategy["strat-b"]["trades"] == 1
    assert review.by_strategy["strat-b"]["win_rate"] == 100.0


def test_monthly_review_empty_trades():
    review = monthly_review([], month="2026-07")
    assert review.total_trades == 0
    assert review.sharpe_ratio == 0.0


def test_monthly_review_computes_drawdown_and_sharpe():
    trades = [
        make_closed_trade(pnl=500.0),
        make_closed_trade(pnl=-1500.0),
        make_closed_trade(pnl=800.0),
    ]
    review = monthly_review(trades, month="2026-07", starting_equity=10_000.0)
    assert review.total_trades == 3
    assert review.total_pnl == -200.0
    assert review.max_drawdown_pct > 0
    assert review.sharpe_ratio != 0.0


def test_monthly_review_zero_drawdown_gives_zero_calmar():
    trades = [make_closed_trade(pnl=100.0), make_closed_trade(pnl=50.0)]
    review = monthly_review(trades, month="2026-07")
    assert review.max_drawdown_pct == 0.0
    assert review.calmar_ratio == 0.0
