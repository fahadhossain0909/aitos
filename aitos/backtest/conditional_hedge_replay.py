"""Replay conditional hedging against an identical no-hedge baseline."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from aitos.backtest.clickhouse_source import (
    ClickHouseHistoricalSource,
    parse_optional_time,
)
from aitos.intelligence.hedge_intelligence import HedgeDecision
from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.trading.position_manager import PositionManager


@dataclass
class RunMetrics:
    final_cash: float
    net_pnl: float
    max_drawdown: float
    mae: float
    mfe: float
    expectancy: float


@dataclass
class RunResult:
    symbol: str
    side: str
    start: str
    end: str
    initial_cash: float
    baseline: RunMetrics
    hedged: RunMetrics
    delta_net_pnl: float
    delta_max_drawdown: float
    delta_mae: float
    delta_mfe: float
    hedge_pnl: float
    hedge_cost: float
    hedge_slippage_cost: float
    hedge_funding_cost: float
    hedge_net_contribution: float
    hedge_count: int
    hedge_open_count: int
    hedge_close_count: int


def _drawdown(equity: list[float]) -> float:
    peak = equity[0] if equity else 0.0
    result = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            result = max(result, (peak - value) / peak)
    return result


def run(args: argparse.Namespace) -> RunResult:
    start = parse_optional_time(args.start)
    end = parse_optional_time(args.end)
    source = ClickHouseHistoricalSource(
        args.host, args.port, args.user, args.password, args.database
    )
    try:
        events = iter(
            source.events(args.symbol, start, end, "trades", args.timeframe)
        )
        first = next(events, None)
        if first is None:
            raise RuntimeError("No historical trade events returned by ClickHouse")
        entry = float(first.price)
        side = TradeSide[args.side]
        direction = 1 if side == TradeSide.LONG else -1
        sl = (
            entry * (1.0 - args.stop_pct)
            if direction == 1
            else entry * (1.0 + args.stop_pct)
        )
        tp = (
            entry * (1.0 + args.take_profit_pct)
            if direction == 1
            else entry * (1.0 - args.take_profit_pct)
        )
        trade = Trade(
            trade_id=f"hedge-replay-{args.symbol.lower()}",
            symbol=args.symbol,
            side=side,
            entry_price=entry,
            quantity=args.quantity,
            leverage=args.leverage,
            position_size_usd=entry * args.quantity,
            risk_amount_usd=abs(entry - sl) * args.quantity,
            strategy_id="conditional-hedge-overlay",
            agent_consensus={},
            explanation="Historical hedge overlay benchmark",
            sl_price=sl,
            tp_price=tp,
            state=TradeLifecycleState.POSITION_OPENED,
            entry_time=first.timestamp.isoformat(),
        )
        pm = PositionManager(
            config={
                "hedge_enabled": True,
                "hedge_max_ratio": args.max_hedge_ratio,
                "hedge_min_ratio": args.min_hedge_ratio,
                "hedge_min_benefit_cost_ratio": args.min_benefit_cost_ratio,
                "hedge_estimated_roundtrip_cost_rate": (
                    2 * args.fee_rate
                    + 2 * args.slippage_bps / 10_000
                    + args.funding_rate_per_8h * 0.125
                ),
            }
        )
        baseline_equity = [args.initial_cash]
        hedged_equity = [args.initial_cash]
        hedge_open_price = None
        hedge_open_time = None
        hedge_ratio = 0.0
        hedge_pnl = 0.0
        hedge_cost = 0.0
        hedge_slippage_cost = 0.0
        hedge_funding_cost = 0.0
        hedge_count = hedge_open_count = hedge_close_count = 0
        prices = [entry]
        primary_fee = abs(entry * args.quantity) * args.fee_rate

        for event in events:
            price = float(event.price)
            prices.append(price)
            trade.record_excursion(price)
            action = pm.evaluate(
                trade=trade, current_price=price, timestamp=event.timestamp
            )
            hd: HedgeDecision | None = action.hedge_decision
            primary_move = (price - entry) * args.quantity * direction
            if hd and hd.action == "OPEN" and hedge_open_price is None:
                hedge_open_price = price
                hedge_open_time = event.timestamp
                hedge_ratio = hd.hedge_ratio
                hedge_count += 1
                hedge_open_count += 1
                hedge_cost += price * args.quantity * hedge_ratio * args.fee_rate
                hedge_slippage_cost += (
                    price
                    * args.quantity
                    * hedge_ratio
                    * args.slippage_bps
                    / 10_000
                )
            elif hd and hd.action == "CLOSE" and hedge_open_price is not None:
                hedge_move = (
                    (hedge_open_price - price) * args.quantity * hedge_ratio
                    if direction == 1
                    else (price - hedge_open_price) * args.quantity * hedge_ratio
                )
                hedge_pnl += hedge_move
                hedge_cost += price * args.quantity * hedge_ratio * args.fee_rate
                hedge_slippage_cost += (
                    price
                    * args.quantity
                    * hedge_ratio
                    * args.slippage_bps
                    / 10_000
                )
                duration_hours = max(
                    (event.timestamp - hedge_open_time).total_seconds() / 3600.0,
                    0.0,
                )
                hedge_funding_cost += (
                    price
                    * args.quantity
                    * hedge_ratio
                    * args.funding_rate_per_8h
                    * duration_hours
                    / 8.0
                )
                hedge_open_price = None
                hedge_open_time = None
                hedge_ratio = 0.0
                hedge_close_count += 1

            baseline_equity.append(args.initial_cash + primary_move - primary_fee)
            active_hedge = 0.0
            if hedge_open_price is not None:
                active_hedge = (
                    (hedge_open_price - price) * args.quantity * hedge_ratio
                    if direction == 1
                    else (price - hedge_open_price) * args.quantity * hedge_ratio
                )
            hedged_equity.append(
                args.initial_cash
                + primary_move
                + hedge_pnl
                + active_hedge
                - hedge_cost
                - hedge_slippage_cost
                - hedge_funding_cost
                - primary_fee
            )

        if hedge_open_price is not None:
            price = prices[-1]
            hedge_move = (
                (hedge_open_price - price) * args.quantity * hedge_ratio
                if direction == 1
                else (price - hedge_open_price) * args.quantity * hedge_ratio
            )
            hedge_pnl += hedge_move
            hedge_cost += price * args.quantity * hedge_ratio * args.fee_rate
            hedge_slippage_cost += (
                price * args.quantity * hedge_ratio * args.slippage_bps / 10_000
            )
            duration_hours = max(
                (end - hedge_open_time).total_seconds() / 3600.0
                if end and hedge_open_time
                else 0.0,
                0.0,
            )
            hedge_funding_cost += (
                price
                * args.quantity
                * hedge_ratio
                * args.funding_rate_per_8h
                * duration_hours
                / 8.0
            )
            hedge_close_count += 1

        primary_move_final = (prices[-1] - entry) * args.quantity * direction
        final_baseline = args.initial_cash + primary_move_final - primary_fee
        total_hedge_cost = hedge_cost + hedge_slippage_cost + hedge_funding_cost
        final_hedged = final_baseline + hedge_pnl - total_hedge_cost
        baseline_pnl = final_baseline - args.initial_cash
        hedged_pnl = final_hedged - args.initial_cash
        baseline_mae = trade.mae_r or 0.0
        baseline_mfe = trade.mfe_r or 0.0
        baseline_metrics = RunMetrics(
            final_cash=final_baseline,
            net_pnl=baseline_pnl,
            max_drawdown=_drawdown(baseline_equity),
            mae=baseline_mae,
            mfe=baseline_mfe,
            expectancy=baseline_pnl,
        )
        hedged_metrics = RunMetrics(
            final_cash=final_hedged,
            net_pnl=hedged_pnl,
            max_drawdown=_drawdown(hedged_equity),
            mae=baseline_mae,
            mfe=baseline_mfe,
            expectancy=hedged_pnl,
        )
        return RunResult(
            symbol=args.symbol,
            side=args.side,
            start=first.timestamp.isoformat(),
            end=end.isoformat() if end else datetime.now(timezone.utc).isoformat(),
            initial_cash=args.initial_cash,
            baseline=baseline_metrics,
            hedged=hedged_metrics,
            delta_net_pnl=hedged_pnl - baseline_pnl,
            delta_max_drawdown=(
                hedged_metrics.max_drawdown - baseline_metrics.max_drawdown
            ),
            delta_mae=hedged_metrics.mae - baseline_metrics.mae,
            delta_mfe=hedged_metrics.mfe - baseline_metrics.mfe,
            hedge_pnl=hedge_pnl,
            hedge_cost=total_hedge_cost,
            hedge_slippage_cost=hedge_slippage_cost,
            hedge_funding_cost=hedge_funding_cost,
            hedge_net_contribution=hedge_pnl - total_hedge_cost,
            hedge_count=hedge_count,
            hedge_open_count=hedge_open_count,
            hedge_close_count=hedge_close_count,
        )
    finally:
        source.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--side", choices=("LONG", "SHORT"), default="LONG")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--timeframe", default="1m")
    p.add_argument("--initial-cash", type=float, default=10_000.0)
    p.add_argument("--quantity", type=float, default=1.0)
    p.add_argument("--leverage", type=float, default=1.0)
    p.add_argument("--fee-rate", type=float, default=0.0004)
    p.add_argument("--slippage-bps", type=float, default=2.0)
    p.add_argument("--funding-rate-per-8h", type=float, default=0.0001)
    p.add_argument("--stop-pct", type=float, default=0.01)
    p.add_argument("--take-profit-pct", type=float, default=0.02)
    p.add_argument("--min-hedge-ratio", type=float, default=0.20)
    p.add_argument("--max-hedge-ratio", type=float, default=0.50)
    p.add_argument("--min-benefit-cost-ratio", type=float, default=2.0)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=8123)
    p.add_argument("--user", default="default")
    p.add_argument("--password", default="")
    p.add_argument("--database", default="aitos")
    args = p.parse_args()
    print(json.dumps(asdict(run(args)), indent=2))


if __name__ == "__main__":
    main()
