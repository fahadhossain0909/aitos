"""Reproducible Python-vs-Rust order-book microbenchmark.

Run from repository root:
    python benchmarks/benchmark_native_orderbook.py --updates 20000 --repeats 7

This is a benchmark harness, not a CI performance assertion. It reports
end-to-end batch replay time for equivalent fixed-point books. Compare on the
same host, Python version, CPU governor, and build profile. Never infer a
production latency win from a single run.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from typing import Callable

from aitos.native_core import create_order_book, native_available


def build_updates(count: int):
    updates = []
    previous = None
    for index in range(count):
        update_id = 101 + index
        first_id = update_id if index else 100
        final_id = update_id
        bids = [(10_000 + index % 128, 1 + index % 100)]
        asks = [(10_200 + index % 128, 2 + index % 100)]
        updates.append((first_id, final_id, bids, asks, previous))
        previous = final_id
    return updates


def run_once(factory: Callable, updates, depth: int) -> int:
    started = time.perf_counter_ns()
    book = factory(100, [(10_000, 10)], [(10_200, 10)])
    applied = book.apply_deltas(updates)
    if applied != len(updates):
        raise RuntimeError(f"applied {applied} of {len(updates)} updates")
    snapshot = book.snapshot(depth)
    if snapshot[0] != 100 + len(updates):
        raise RuntimeError("final order-book sequence id is incorrect")
    return time.perf_counter_ns() - started


def benchmark(factory: Callable, updates, depth: int, repeats: int) -> dict:
    durations = [run_once(factory, updates, depth) for _ in range(repeats)]
    ordered = sorted(durations)
    p95_index = min(len(ordered) - 1, int(0.95 * len(ordered)))
    median_ns = statistics.median(durations)
    return {
        "median_ms": round(median_ns / 1_000_000, 3),
        "p95_ms": round(ordered[p95_index] / 1_000_000, 3),
        "updates_per_second": round(
            len(updates) * 1_000_000_000 / median_ns, 1
        ),
        "samples": repeats,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--updates", type=int, default=20_000)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--depth", type=int, default=100)
    args = parser.parse_args()
    if args.updates < 1 or args.repeats < 3 or args.depth < 1:
        parser.error("updates/depth must be positive and repeats must be >= 3")

    updates = build_updates(args.updates)
    results = {
        "python_reference": benchmark(
            lambda snapshot, bids, asks: create_order_book(
                snapshot, bids, asks, prefer_native=False
            ),
            updates,
            args.depth,
            args.repeats,
        )
    }
    if native_available():
        results["rust_native"] = benchmark(
            lambda snapshot, bids, asks: create_order_book(
                snapshot, bids, asks, prefer_native=True
            ),
            updates,
            args.depth,
            args.repeats,
        )
        python_rate = results["python_reference"]["updates_per_second"]
        native_rate = results["rust_native"]["updates_per_second"]
        results["native_speedup"] = round(native_rate / python_rate, 3)
    else:
        results["rust_native"] = {
            "available": False,
            "reason": "optional aitos_native extension is not installed",
        }

    print(
        json.dumps(
            {
                "benchmark": "aitos_fixed_point_orderbook_batch_replay",
                "updates": args.updates,
                "depth": args.depth,
                "results": results,
                "note": (
                    "Microbenchmark only; use production-like replay and "
                    "end-to-end p50/p95/p99 before selecting a backend."
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
