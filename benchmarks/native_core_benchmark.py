"""Reproducible local microbenchmark; not an end-to-end latency claim."""

import argparse
import math
import statistics
import time

from aitos.native_core import (
    PythonFixedPointOrderBook,
    create_order_book,
    native_available,
)


def percentile(samples, quantile):
    ordered = sorted(samples)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def run(factory, events, repeats, warmup):
    elapsed_samples = []
    latency_samples = []
    for _ in range(repeats):
        book = factory()
        for index in range(warmup):
            update_id = index + 11
            book.apply_delta(
                update_id,
                update_id,
                bids=[(100_000 + index % 100, 1 + index % 7)],
                asks=[(100_200 + index % 100, 1 + index % 5)],
                previous_update_id=update_id - 1 if index else None,
            )

        latencies = []
        started = time.perf_counter()
        for index in range(events):
            update_id = warmup + index + 11
            event_started = time.perf_counter_ns()
            book.apply_delta(
                update_id,
                update_id,
                bids=[(100_000 + index % 100, 1 + index % 7)],
                asks=[(100_200 + index % 100, 1 + index % 5)],
                previous_update_id=update_id - 1,
            )
            latencies.append(time.perf_counter_ns() - event_started)
        elapsed_samples.append(time.perf_counter() - started)
        latency_samples.extend(latencies)
    return elapsed_samples, latency_samples


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=100_000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=5_000)
    args = parser.parse_args()
    if args.events <= 0 or args.repeats <= 0 or args.warmup < 0:
        parser.error("events/repeats must be positive and warmup non-negative")

    factories = {
        "python-reference": lambda: PythonFixedPointOrderBook(
            10, [(99_900, 1)], [(100_100, 1)]
        )
    }
    if native_available():
        factories["rust-native"] = lambda: create_order_book(
            10, [(99_900, 1)], [(100_100, 1)]
        )

    print(
        f"events={args.events} repeats={args.repeats} warmup={args.warmup} "
        f"native_available={native_available()}"
    )
    for name, factory in factories.items():
        elapsed, latencies = run(
            factory, args.events, args.repeats, args.warmup
        )
        median_elapsed = statistics.median(elapsed)
        print(
            f"{name}: median_total={median_elapsed:.6f}s "
            f"throughput={args.events / median_elapsed:.0f} events/s "
            f"p50={percentile(latencies, 0.50) / 1_000:.2f}us "
            f"p95={percentile(latencies, 0.95) / 1_000:.2f}us "
            f"p99={percentile(latencies, 0.99) / 1_000:.2f}us "
            f"p99.9={percentile(latencies, 0.999) / 1_000:.2f}us"
        )


if __name__ == "__main__":
    main()
