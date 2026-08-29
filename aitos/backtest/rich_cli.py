"""CLI for the full AITOS L2/futures historical replay engine."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aitos.backtest.aitos_runner import AITOSHistoricalRunner, HistoricalDecision
from aitos.models.market import OrderBookSnapshot, TradeTick


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        number = float(value)
        dt = datetime.fromtimestamp(
            number / 1000.0 if abs(number) > 10_000_000_000 else number, tz=timezone.utc
        )
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_decision(spec: str) -> Callable[[Any], HistoricalDecision]:
    module_name, separator, attr = spec.partition(":")
    if not separator:
        module_name, separator, attr = spec.rpartition(".")
    if not module_name or not attr:
        raise ValueError("Decision strategy must be specified as module:function")
    decision = getattr(importlib.import_module(module_name), attr)
    if not callable(decision):
        raise TypeError(f"Decision strategy is not callable: {spec}")
    return decision


def auction_decision(state: Any) -> HistoricalDecision:
    """Small deterministic baseline using the shared auction scores.

    This is intentionally a conservative baseline, not a production strategy.
    Real strategy modules should implement the same state -> decision contract.
    """
    long_score = float(state.auction_long_score)
    short_score = float(state.auction_short_score)
    if max(long_score, short_score) < 0.55:
        return HistoricalDecision("flat", 0.0, 0.0)
    if long_score > short_score:
        return HistoricalDecision("long", long_score, 1.0)
    return HistoricalDecision("short", short_score, 1.0)


def _row_to_event(row: dict[str, Any]) -> TradeTick | OrderBookSnapshot:
    kind = str(row.get("event_type", row.get("type", ""))).lower()
    if kind in {"trade", "trade_tick", "tick"}:
        data = dict(row)
        data["timestamp"] = _timestamp(data.get("timestamp", data.get("time")))
        return TradeTick.from_dict(data)
    if kind in {"orderbook", "order_book", "order_book_snapshot", "book"}:
        data = dict(row)
        data["timestamp"] = _timestamp(data.get("timestamp", data.get("time")))
        return OrderBookSnapshot.from_dict(data)
    raise ValueError(
        "Each rich historical row must contain event_type=trade or event_type=orderbook"
    )


def read_market_events(
    path: str | Path, fmt: str = "auto"
) -> Iterator[TradeTick | OrderBookSnapshot]:
    source = Path(path)
    if fmt == "auto":
        fmt = (
            "parquet"
            if source.is_dir() or source.suffix.lower() in {".parquet", ".pq"}
            else "jsonl"
        )
    if fmt == "jsonl":
        with source.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    yield _row_to_event(json.loads(line))
                except Exception as exc:
                    raise ValueError(
                        f"Invalid rich JSONL row at line {line_no}: {exc}"
                    ) from exc
        return
    if fmt == "parquet":
        try:
            import pyarrow.dataset as ds
        except ImportError as exc:
            raise RuntimeError("Parquet input requires pyarrow") from exc
        for batch in (
            ds.dataset(str(source), format="parquet", partitioning="hive")
            .scanner(batch_size=50_000)
            .to_batches()
        ):
            for row in batch.to_pylist():
                yield _row_to_event(row)
        return
    raise ValueError(f"Unsupported input format: {fmt}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the full AITOS L2/futures historical replay"
    )
    parser.add_argument(
        "--source", choices=("clickhouse", "file"), default="clickhouse"
    )
    parser.add_argument("--data")
    parser.add_argument(
        "--format", choices=("auto", "jsonl", "parquet"), default="auto"
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--tick-size", type=float, required=True)
    parser.add_argument(
        "--decision-strategy", default="aitos.backtest.rich_cli:auction_decision"
    )
    parser.add_argument("--initial-cash", type=float, default=10_000.0)
    parser.add_argument("--fee-rate", type=float, default=0.0004)
    parser.add_argument("--slippage-bps", type=float, default=0.0)
    parser.add_argument("--leverage", type=float, default=1.0)
    parser.add_argument("--maintenance-rate", type=float, default=0.005)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--trade-window", type=int, default=500)
    parser.add_argument("--max-book-levels", type=int)
    parser.add_argument(
        "--clickhouse-host", default=os.getenv("CLICKHOUSE_HOST", "localhost")
    )
    parser.add_argument(
        "--clickhouse-port", type=int, default=int(os.getenv("CLICKHOUSE_PORT", "8123"))
    )
    parser.add_argument(
        "--clickhouse-user", default=os.getenv("CLICKHOUSE_USER", "default")
    )
    parser.add_argument(
        "--clickhouse-password", default=os.getenv("CLICKHOUSE_PASSWORD", "")
    )
    parser.add_argument("--clickhouse-db", default=os.getenv("CLICKHOUSE_DB", "aitos"))
    return parser


def _parse_optional(value: str | None) -> datetime | None:
    return _timestamp(value) if value else None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    decide = load_decision(args.decision_strategy)
    source = None
    if args.source == "clickhouse":
        from .clickhouse_market_source import ClickHouseMarketEventSource

        source = ClickHouseMarketEventSource(
            args.clickhouse_host,
            args.clickhouse_port,
            args.clickhouse_user,
            args.clickhouse_password,
            args.clickhouse_db,
        )
        events: Iterable[TradeTick | OrderBookSnapshot] = source.events(
            args.symbol, _parse_optional(args.start), _parse_optional(args.end)
        )
    else:
        if not args.data:
            raise SystemExit("--data is required when --source=file")
        events = read_market_events(args.data, args.format)

    runner = AITOSHistoricalRunner(
        symbol=args.symbol,
        tick_size=args.tick_size,
        initial_cash=args.initial_cash,
        fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
        trade_window=args.trade_window,
        max_book_levels=args.max_book_levels,
        leverage=args.leverage,
        maintenance_rate=args.maintenance_rate,
    )
    try:
        result = runner.run(events, decide)
    finally:
        if source is not None:
            source.close()
    payload = {
        "engine": "aitos_l2_futures",
        "source": args.source,
        "symbol": args.symbol,
        "decision_strategy": args.decision_strategy,
        "states": result.states,
        "decisions": result.decisions,
        "fills": result.fills,
        "requested_quantity": result.requested_quantity,
        "filled_quantity": result.filled_quantity,
        "initial_equity": args.initial_cash,
        "final_equity": result.final_equity,
        "total_return": result.total_return,
        "total_fees": result.total_fees,
        "funding_paid": result.funding_paid,
        "liquidated": result.liquidated,
        "passive_orders": result.passive_orders,
        "passive_fills": result.passive_fills,
    }
    print(json.dumps(payload, indent=2, allow_nan=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
