"""Run a paired canonical PositionManager hedge benchmark from ClickHouse."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from aitos.backtest.clickhouse_market_source import ClickHouseMarketEventSource
from aitos.backtest.position_manager_replay import replay


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def result_dict(result):
    return {
        "hedge_enabled": result.hedge_enabled,
        "states": result.states,
        "primary_entries": result.primary_entries,
        "primary_exits": result.primary_exits,
        "hedge_opens": result.hedge_opens,
        "hedge_closes": result.hedge_closes,
        "initial_equity": result.initial_equity,
        "final_equity": result.final_equity,
        "net_pnl": result.net_pnl,
        "total_return": result.total_return,
        "total_fees": result.total_fees,
        "hedge_fees": result.hedge_fees,
        "hedge_execution_cost": result.hedge_execution_cost,
        "hedge_pnl": result.hedge_pnl,
        "max_drawdown": result.max_drawdown,
        "expectancy": result.expectancy,
        "mae": result.mae,
        "mfe": result.mfe,
        "hedge_durations_seconds": list(result.hedge_durations_seconds),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--tick-size", type=float, required=True)
    p.add_argument("--initial-cash", type=float, default=10_000.0)
    p.add_argument("--fee-rate", type=float, default=0.0004)
    p.add_argument("--trade-window", type=int, default=500)
    p.add_argument("--max-book-levels", type=int)
    p.add_argument("--output", required=True)
    args = p.parse_args()

    start = parse_dt(args.start)
    end = parse_dt(args.end)
    source = ClickHouseMarketEventSource()
    try:
        events = list(source.events(args.symbol, start, end))
    finally:
        source.close()
    if not events:
        raise SystemExit("No historical events returned from ClickHouse")

    common = dict(
        symbol=args.symbol,
        tick_size=args.tick_size,
        initial_cash=args.initial_cash,
        fee_rate=args.fee_rate,
        trade_window=args.trade_window,
        max_book_levels=args.max_book_levels,
    )
    baseline = replay(events, **common, hedge_enabled=False)
    hedged = replay(events, **common, hedge_enabled=True)

    payload = {
        "schema_version": 1,
        "engine": "aitos_position_manager_hedge_replay",
        "symbol": args.symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "event_count": len(events),
        "baseline": result_dict(baseline),
        "hedged": result_dict(hedged),
        "comparison": {
            "net_pnl_delta": hedged.net_pnl - baseline.net_pnl,
            "drawdown_reduction": (
                (baseline.max_drawdown - hedged.max_drawdown) / baseline.max_drawdown
                if baseline.max_drawdown else 0.0
            ),
            "expectancy_delta": hedged.expectancy - baseline.expectancy,
            "mae_delta": hedged.mae - baseline.mae,
            "mfe_delta": hedged.mfe - baseline.mfe,
            "hedge_net_contribution": hedged.hedge_pnl - hedged.hedge_fees - hedged.hedge_execution_cost,
        },
    }
    Path(args.output).write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(payload, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
