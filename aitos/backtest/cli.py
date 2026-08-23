"""CLI for canonical ProjectAlpha historical backtests."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from aitos.learning.clickhouse_store import ClickHouseExperienceStore
from aitos.learning.experience import ExperienceRecord

from .engine import BacktestEngine


@dataclass(frozen=True)
class HistoricalEvent:
    timestamp: datetime
    price: float
    fields: dict[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self.fields[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        seconds = (
            float(value) / 1000.0
            if abs(float(value)) > 10_000_000_000
            else float(value)
        )
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _event(row: dict[str, Any]) -> HistoricalEvent:
    if "timestamp" not in row or "price" not in row:
        raise ValueError("Each historical row must contain 'timestamp' and 'price'")
    return HistoricalEvent(_timestamp(row["timestamp"]), float(row["price"]), row)


def read_events(path: str | Path, fmt: str = "auto") -> Iterator[HistoricalEvent]:
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
                    yield _event(json.loads(line))
                except Exception as exc:
                    raise ValueError(
                        f"Invalid JSONL row at line {line_no}: {exc}"
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
                yield _event(row)
        return
    raise ValueError(f"Unsupported input format: {fmt}")


def load_strategy(spec: str) -> Callable[[Any, Any], None]:
    module_name, separator, attr = spec.partition(":")
    if not separator:
        module_name, separator, attr = spec.rpartition(".")
    if not module_name or not attr:
        raise ValueError("Strategy must be specified as module:function")
    strategy = getattr(importlib.import_module(module_name), attr)
    if not callable(strategy):
        raise TypeError(f"Strategy is not callable: {spec}")
    return strategy


def buy_and_hold(event: Any, execution: Any) -> None:
    if not getattr(execution, "_cli_bought", False):
        execution.execute("buy", 1.0, float(event.price))
        execution._cli_bought = True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a canonical ProjectAlpha historical backtest"
    )
    parser.add_argument("--data")
    parser.add_argument("--source", choices=("file", "clickhouse"), default="file")
    parser.add_argument(
        "--format", choices=("auto", "jsonl", "parquet"), default="auto"
    )
    parser.add_argument("--strategy", default="aitos.backtest.cli:buy_and_hold")
    parser.add_argument("--initial-cash", type=float, default=10_000.0)
    parser.add_argument("--fee-rate", type=float, default=0.0004)
    parser.add_argument("--slippage-bps", type=float, default=0.0)
    parser.add_argument("--symbol", default=None)
    parser.add_argument(
        "--table", choices=("ohlcv", "trades", "orderbook"), default="ohlcv"
    )
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--start")
    parser.add_argument("--end")
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
    parser.add_argument(
        "--persist-learning",
        action="store_true",
        help="persist a summary and realized trade experiences to ClickHouse",
    )
    return parser


def _persist_summary(args, result) -> None:
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=args.clickhouse_host,
        port=args.clickhouse_port,
        username=args.clickhouse_user,
        password=args.clickhouse_password,
        database=args.clickhouse_db,
    )
    store = ClickHouseExperienceStore(client, args.clickhouse_db)
    store.ensure_schema()
    m = result.metrics
    records = [
        ExperienceRecord(
            timestamp=datetime.now(timezone.utc),
            source="backtest",
            symbol=args.symbol or "unknown",
            decision="backtest_summary",
            outcome="completed",
            reward=m.total_return,
            confidence=1.0,
            strategy_version=args.strategy,
            metadata={
                "initial_equity": m.initial_equity,
                "final_equity": m.final_equity,
                "max_drawdown": m.max_drawdown,
                "sharpe": m.sharpe,
                "trades": m.trades,
                "wins": m.wins,
                "losses": m.losses,
                "win_rate": m.win_rate,
                "profit_factor": m.profit_factor,
            },
        )
    ]
    for trade in result.trades:
        records.append(
            ExperienceRecord(
                timestamp=trade.timestamp,
                source="backtest",
                symbol=args.symbol or str(trade.fields.get("symbol", "unknown")),
                decision="backtest_trade",
                outcome="closed",
                reward=float(trade.reward),
                confidence=1.0,
                price=trade.price,
                features={
                    k: v
                    for k, v in trade.fields.items()
                    if isinstance(v, (int, float, bool))
                },
                strategy_version=args.strategy,
                metadata={"event_type": "backtest_trade"},
            )
        )
    store.append(records)
    client.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    strategy = load_strategy(args.strategy)
    source = None
    if args.source == "file":
        if not args.data:
            raise SystemExit("--data is required when --source=file")
        events = read_events(args.data, args.format)
    else:
        if not args.symbol:
            raise SystemExit("--symbol is required when --source=clickhouse")
        from .clickhouse_source import (ClickHouseHistoricalSource,
                                        parse_optional_time)

        source = ClickHouseHistoricalSource(
            args.clickhouse_host,
            args.clickhouse_port,
            args.clickhouse_user,
            args.clickhouse_password,
            args.clickhouse_db,
        )
        events = source.events(
            args.symbol,
            parse_optional_time(args.start),
            parse_optional_time(args.end),
            args.table,
            args.timeframe,
        )
    try:
        result = BacktestEngine(
            initial_cash=args.initial_cash,
            fee_rate=args.fee_rate,
            slippage_bps=args.slippage_bps,
        ).run(events, strategy, lambda event: float(event.price))
    finally:
        if source is not None:
            source.close()
    if args.persist_learning:
        _persist_summary(args, result)
    m = result.metrics
    print(
        json.dumps(
            {
                "source": args.source,
                "symbol": args.symbol,
                "strategy": args.strategy,
                "events": len(result.equity_curve),
                "initial_equity": m.initial_equity,
                "final_equity": m.final_equity,
                "total_return": m.total_return,
                "max_drawdown": m.max_drawdown,
                "sharpe": m.sharpe,
                "total_fees": m.total_fees,
                "trades": m.trades,
                "wins": m.wins,
                "losses": m.losses,
                "win_rate": m.win_rate,
                "profit_factor": m.profit_factor,
            },
            indent=2,
            allow_nan=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
