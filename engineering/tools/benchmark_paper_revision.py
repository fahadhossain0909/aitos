"""Synthetic local microbenchmark, NOT production SLO or trading performance.

Run from the repository root with .venv/bin/python. Only its own temporary file
is created/removed; there is no option to open an existing financial journal.
"""

from __future__ import annotations

import json
import math
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.trading.paper_revision import PaperRevisionStore
from aitos.trading.revision_journal import RevisionJournal, Scope


def summary(values):
    ordered = sorted(values)

    def percentile(p):
        return ordered[max(0, math.ceil(p * len(ordered)) - 1)]

    return dict(
        samples=len(values),
        p50_ms=statistics.median(ordered),
        p95_ms=percentile(0.95),
        p99_ms=percentile(0.99),
        max_ms=max(ordered),
    )


def run():
    temporary = ROOT / "tmp"
    temporary.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="synthetic-paper-benchmark-", dir=temporary
    ) as directory:
        journal = RevisionJournal(Path(directory) / "synthetic-only.sqlite")
        writer = journal.acquire(
            Scope("paper", "synthetic", "benchmark", "run"),
            "writer",
            expected_generation=0,
        )
        store = PaperRevisionStore(journal, writer)
        writes = []
        reads = []
        for revision in range(5):
            for i in range(20):
                trade = Trade(
                    trade_id=f"synthetic-{i}",
                    symbol="BTCUSDT",
                    side=TradeSide.LONG,
                    entry_price=100,
                    quantity=1,
                    leverage=1,
                    position_size_usd=100,
                    risk_amount_usd=2,
                    strategy_id="synthetic",
                    agent_consensus={},
                    explanation="not real financial data",
                    sl_price=98,
                    tp_price=110,
                    state=TradeLifecycleState.POSITION_OPENED,
                    entry_time="2026-11-01T00:00:00Z",
                    updated_at=f"2026-11-01T00:00:0{revision}Z",
                )
                before = time.perf_counter_ns()
                store.save_trade(
                    trade, event_id=f"event-{i}-{revision}", expected_revision=revision
                )
                writes.append((time.perf_counter_ns() - before) / 1e6)
        for _ in range(50):
            before = time.perf_counter_ns()
            recovered = store.recover()
            assert len(recovered.open_trades) == 20
            reads.append((time.perf_counter_ns() - before) / 1e6)
        return dict(
            schema_version=1,
            synthetic=True,
            production_certified=False,
            environment=dict(
                python=platform.python_version(),
                sqlite=sqlite3.sqlite_version,
                platform=platform.platform(),
                clock="perf_counter_ns",
                wal=True,
                synchronous="FULL",
            ),
            workload=dict(
                trades=20,
                revisions_per_trade=5,
                append_samples=100,
                recovery_samples=50,
                recovery_includes="all 100 history events plus 20 typed heads; decoded open trades",
            ),
            append=summary(writes),
            recovery=summary(reads),
            limitations=[
                "single process, no competing writer",
                "sandbox filesystem; no power-loss or disk-full test",
                "no market feed, venue, Redis, outbox network sink or production workload",
                "no warmup subtraction; local microbenchmark only",
            ],
        )


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, allow_nan=False))
