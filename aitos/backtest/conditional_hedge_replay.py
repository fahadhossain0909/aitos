"""Replay the canonical PositionManager hedge overlay on real ClickHouse prices.

This is intentionally an overlay benchmark: it keeps one primary position fixed
for the selected window so the isolated value/cost of conditional hedging can
be measured without inventing an entry strategy. A future full-strategy replay
can replace the primary-position generator without changing the hedge engine.
"""

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
class RunResult:
    symbol: str
    side: str
    start: str
    end: str
    initial_cash: float
    final_cash: float
    net_pnl: float
    hedge_pnl: float
    hedge_cost: float
    hedge_count: int
    hedge_open_count: int
    hedge_close_count: int
    max_drawdown: float
    mae: float
    mfe: float
    expectancy: float


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
        events = source.events(args.symbol, start, end, "trades", args.timeframe)
        first = next(iter(events), None)
        if first is None:
            raise RuntimeError("No historical trade events returned by ClickHouse")
        entry = float(first.price)
        side = TradeSide[args.side]
        sl = (
            entry * (1.0 - args.stop_pct)
            if side == TradeSide.LONG
            else entry * (1.0 + args.stop_pct)
        )
        tp = (
            entry * (1.0 + args.take_profit_pct)
            if side == TradeSide.LONG
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
            }
        )
        baseline_equity = [args.initial_cash]
        hedged_equity = [args.initial_cash]
        hedge_open_price = None
        hedge_ratio = 0.0
        hedge_pnl = 0.0
        hedge_cost = 0.0
        hedge_count = hedge_open_count = hedge_close_count = 0
        prices = [entry]
        equity = args.initial_cash
        for event in events:
            price = float(event.price)
            prices.append(price)
            trade.record_excursion(price)
            action = pm.evaluate(
                trade=trade,
                current_price=price,
                timestamp=event.timestamp,
            )
            hd: HedgeDecision | None = action.hedge_decision
            primary_move = (price - entry) * args.quantity * (
                1 if side == TradeSide.LONG else -1
            )
            if hd and hd.action == "OPEN" and hedge_open_price is None:
                hedge_open_price = price
                hedge_ratio = hd.hedge_ratio
                hedge_count += 1
                hedge_open_count += 1
                hedge_cost += price * args.quantity * hedge_ratio * args.fee_rate
            elif hd and hd.action == "CLOSE" and hedge_open_price is not None:
                hedge_move = (
                    (hedge_open_price - price) * args.quantity * hedge_ratio
                    if side == TradeSide.LONG
                    else (price - hedge_open_price) * args.quantity * hedge_ratio
                )
                hedge_pnl += hedge_move
                hedge_cost += price * args.quantity * hedge_ratio * args.fee_rate
                hedge_open_price = None
                hedge_ratio = 0.0
                hedge_close_count += 1
            baseline_equity.append(
                args.initial_cash
                + primary_move
                - abs(entry * args.quantity) * args.fee_rate
            )
            active_hedge = 0.0
            if hedge_open_price is not None:
                active_hedge = (
                    (hedge_open_price - price) * args.quantity * hedge_ratio
                    if side == TradeSide.LONG
                    else (price - hedge_open_price) * args.quantity * hedge_ratio
                )
            hedged_equity.append(
                args.initial_cash
                + primary_move
                + hedge_pnl
                + active_hedge
                - hedge_cost
                - abs(entry * args.quantity) * args.fee_rate
            )
            equity = hedged_equity[-1]
        if hedge_open_price is not None:
            price = prices[-1]
            hedge_move = (
                (hedge_open_price - price) * args.quantity * hedge_ratio
                if side == TradeSide.LONG
                else (price - hedge_open_price) * args.quantity * hedge_ratio
            )
            hedge_pnl += hedge_move
            hedge_cost += price * args.quantity * hedge_ratio * args.fee_rate
            hedge_close_count += 1
        final_hedged = (
            args.initial_cash
            + (prices[-1] - entry)
            * args.quantity
            * (1 if side == TradeSide.LONG else -1)
            + hedge_pnl
            - hedge_cost
            - abs(entry * args.quantity) * args.fee_rate
        )
        final_baseline = (
            args.initial_cash
            + (prices[-1] - entry)
            * args.quantity
            * (1 if side == TradeSide.LONG else -1)
            - abs(entry * args.quantity) * args.fee_rate
        )
        return RunResult(
            args.symbol,
            args.side,
            first.timestamp.isoformat(),
            end.isoformat()
            if end
            else datetime.now(timezone.utc).isoformat(),
            args.initial_cash,
            final_hedged,
            final_hedged - args.initial_cash,
            hedge_pnl,
            hedge_cost,
            hedge_count,
            hedge_open_count,
            hedge_close_count,
            _drawdown(hedged_equity),
            trade.mae_r or 0.0,
            trade.mfe_r or 0.0,
            (final_hedged - args.initial_cash) / max(1, hedge_count),
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
    p.add_argument("--stop-pct", type=float, default=0.01)
    p.add_argument("--take-profit-pct", type=float, default=0.02)
    p.add_argument("--min-hedge-ratio", type=float, default=0.20)
    p.add_argument("--max-hedge-ratio", type=float, default=0.50)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=8123)
    p.add_argument("--user", default="default")
    p.add_argument("--password", default="")
    p.add_argument("--database", default="aitos")
    args = p.parse_args()
    print(json.dumps(asdict(run(args)), indent=2))


if __name__ == "__main__":
    main()
