"""Periodic review computations — spec §34.1:

    Daily: PnL, win rate, R-multiples, mistakes
    Weekly: Strategy performance, regime adaptation, correlation
    Monthly: Sharpe, max drawdown, Calmar, learning progress

All pure functions over a list of closed ``Trade`` objects — real
statistics, not placeholders. "Mistakes identified" and "regime
adaptation"/"correlation" pieces of the spec need either human/Learning
Agent input (mistakes — see ``JournalSystem.record_mistake``) or
cross-trade correlation data this module doesn't have yet; those are
intentionally left out rather than faked.
"""

from __future__ import annotations

import math

from aitos.journal.models import DailyReview, MonthlyReview, WeeklyReview
from aitos.models.trade import Trade


def r_multiple(trade: Trade) -> float:
    """Realized P&L expressed in units of the trade's initial risk."""
    if not trade.risk_amount_usd or trade.pnl is None:
        return 0.0
    return round(trade.pnl / trade.risk_amount_usd, 4)


def _closed_only(trades: list[Trade]) -> list[Trade]:
    return [t for t in trades if t.pnl is not None]


def daily_review(trades: list[Trade], date: str) -> DailyReview:
    closed = _closed_only(trades)
    if not closed:
        return DailyReview(
            date=date,
            total_trades=0,
            wins=0,
            losses=0,
            win_rate=0.0,
            total_pnl=0.0,
            avg_r_multiple=0.0,
            best_trade_pnl=0.0,
            worst_trade_pnl=0.0,
        )

    pnls = [t.pnl for t in closed]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    r_multiples = [r_multiple(t) for t in closed]

    return DailyReview(
        date=date,
        total_trades=len(closed),
        wins=wins,
        losses=losses,
        win_rate=round(wins / len(closed) * 100, 2),
        total_pnl=round(sum(pnls), 2),
        avg_r_multiple=(
            round(sum(r_multiples) / len(r_multiples), 4) if r_multiples else 0.0
        ),
        best_trade_pnl=round(max(pnls), 2),
        worst_trade_pnl=round(min(pnls), 2),
    )


def weekly_review(trades: list[Trade], week_start: str) -> WeeklyReview:
    closed = _closed_only(trades)
    by_strategy: dict[str, dict[str, float]] = {}
    for t in closed:
        bucket = by_strategy.setdefault(
            t.strategy_id, {"trades": 0, "pnl": 0.0, "wins": 0}
        )
        bucket["trades"] += 1
        bucket["pnl"] += t.pnl
        if t.pnl > 0:
            bucket["wins"] += 1

    for stats in by_strategy.values():
        stats["win_rate"] = (
            round(stats["wins"] / stats["trades"] * 100, 2) if stats["trades"] else 0.0
        )
        stats["pnl"] = round(stats["pnl"], 2)
        del stats["wins"]

    total_pnl = sum(t.pnl for t in closed)
    wins = sum(1 for t in closed if t.pnl > 0)

    return WeeklyReview(
        week_start=week_start,
        total_trades=len(closed),
        total_pnl=round(total_pnl, 2),
        win_rate=round(wins / len(closed) * 100, 2) if closed else 0.0,
        by_strategy=by_strategy,
    )


def monthly_review(
    trades: list[Trade], month: str, starting_equity: float = 10_000.0
) -> MonthlyReview:
    closed = _closed_only(trades)
    if not closed:
        return MonthlyReview(
            month=month,
            total_trades=0,
            total_pnl=0.0,
            sharpe_ratio=0.0,
            max_drawdown_pct=0.0,
            calmar_ratio=0.0,
        )

    pnls = [t.pnl for t in closed]
    total_pnl = sum(pnls)

    # Sharpe: mean/stddev of per-trade returns (as a fraction of starting equity),
    # unannualized — a simplified per-trade Sharpe rather than a time-annualized one.
    trade_returns = [p / starting_equity for p in pnls]
    mean_return = sum(trade_returns) / len(trade_returns)
    variance = sum((r - mean_return) ** 2 for r in trade_returns) / len(trade_returns)
    stddev = math.sqrt(variance)
    sharpe = round(mean_return / stddev, 4) if stddev > 0 else 0.0

    # Max drawdown over the equity curve built from cumulative pnl in trade-close order.
    equity_curve = [starting_equity]
    for p in pnls:
        equity_curve.append(equity_curve[-1] + p)
    peak = equity_curve[0]
    max_dd_pct = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            dd = (peak - equity) / peak * 100
            max_dd_pct = max(max_dd_pct, dd)

    calmar = (
        round((total_pnl / starting_equity * 100) / max_dd_pct, 4)
        if max_dd_pct > 0
        else 0.0
    )

    return MonthlyReview(
        month=month,
        total_trades=len(closed),
        total_pnl=round(total_pnl, 2),
        sharpe_ratio=sharpe,
        max_drawdown_pct=round(max_dd_pct, 2),
        calmar_ratio=calmar,
    )
