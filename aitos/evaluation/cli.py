"""CLI for offline exit-policy comparison.

Example (synthetic path):

    python -m aitos.evaluation.cli --demo

Example (CSV bars: timestamp,open,high,low,close[,volume]):

    python -m aitos.evaluation.cli \
        --bars path/to/bars.csv \
        --side LONG --entry 79000 --sl 78500 --tp 80000
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aitos.evaluation.exit_replay import (
    PriceBar,
    TradeScenario,
    compare_policies,
)


def _parse_ts(value: str) -> datetime:
    value = value.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(value.replace("Z", "+0000"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    raise ValueError(f"unrecognised timestamp: {value!r}")


def load_bars_csv(path: Path) -> list[PriceBar]:
    bars: list[PriceBar] = []
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            bars.append(
                PriceBar(
                    timestamp=_parse_ts(row["timestamp"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0.0),
                )
            )
    if not bars:
        raise SystemExit(f"no bars loaded from {path}")
    return bars


def demo_bars(n: int = 40) -> list[PriceBar]:
    """Synthetic uptrend with a mid-path pullback — useful smoke test."""
    base = 79000.0
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars: list[PriceBar] = []
    price = base
    for i in range(n):
        # Impulse up, mild pullback mid-way, then continuation
        if i < 12:
            drift = 40.0
        elif i < 20:
            drift = -25.0
        else:
            drift = 35.0
        o = price
        c = price + drift
        h = max(o, c) + 15.0
        l = min(o, c) - 15.0
        bars.append(
            PriceBar(
                timestamp=t0 + timedelta(minutes=i * 5),
                open=o,
                high=h,
                low=l,
                close=c,
                volume=100.0 + i,
            )
        )
        price = c
    return bars


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Compare static TP/SL vs Exit Intelligence")
    p.add_argument("--demo", action="store_true", help="Run synthetic demo path")
    p.add_argument("--bars", type=Path, help="CSV with timestamp,open,high,low,close")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--side", default="LONG", choices=["LONG", "SHORT"])
    p.add_argument("--entry", type=float, default=79000.0)
    p.add_argument("--sl", type=float, default=78500.0)
    p.add_argument("--tp", type=float, nargs="*", default=[80000.0])
    p.add_argument("--size", type=float, default=10_000.0)
    p.add_argument("--json", action="store_true", help="Emit JSON summary")
    args = p.parse_args(argv)

    if args.demo:
        bars = demo_bars()
    elif args.bars:
        bars = load_bars_csv(args.bars)
    else:
        p.error("provide --demo or --bars PATH")

    scenario = TradeScenario(
        symbol=args.symbol,
        side=args.side,
        entry_price=args.entry,
        stop_loss=args.sl,
        take_profit_levels=tuple(args.tp),
        position_size_usd=args.size,
    )
    summary = compare_policies(scenario, bars)

    if args.json:
        json.dump(summary.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(
            f"Symbol={scenario.symbol} side={scenario.side} entry={scenario.entry_price}"
        )
        print(f"Bars={summary.bars_consumed}")
        print("--- static ---")
        s = summary.static
        print(
            f"  exit={s.exit_price:.4g} reason={s.exit_reason} "
            f"hold={s.hold_bars} pnl={s.pnl_usd:.2f} R={s.r_multiple:.2f}"
        )
        print("--- eie ---")
        e = summary.eie
        print(
            f"  exit={e.exit_price:.4g} reason={e.exit_reason} "
            f"hold={e.hold_bars} pnl={e.pnl_usd:.2f} R={e.r_multiple:.2f}"
        )
        print(
            f"delta(eie-static)={summary.pnl_delta_usd:.2f} "
            f"eie_better={summary.eie_better}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
