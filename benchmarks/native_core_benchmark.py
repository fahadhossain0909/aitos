"""Reproducible batch microbenchmark; not an end-to-end latency claim."""

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


def run(factory, events, repeats, warmup, batch_size):
    elapsed_samples = []
    amortized_latency_samples = []
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

        started = time.perf_counter()
        for batch_start in range(0, events, batch_size):
            batch = []
            batch_end = min(batch_start + batch_size, events)
            for index in range(batch_start, batch_end):
                update_id = warmup + index + 11
                batch.append(
                    (
                        update_id,
                        update_id,
                        [(100_000 + index % 100, 1 + index % 7)],
                        [(100_200 + index % 100, 1 + index % 5)],
                        update_id - 1,
                    )
                )
            batch_started = time.perf_counter_ns()
            applied = book.apply_deltas(batch)
            batch_elapsed = time.perf_counter_ns() - batch_started
            if applied != len(batch):
                raise RuntimeError(
                    f"expected {len(batch)} applied events, received {applied}"
                )
            per_event = batch_elapsed / len(batch)
            amortized_latency_samples.extend([per_event] * len(batch))
        elapsed_samples.append(time.perf_counter() - started)
    return elapsed_samples, amortized_latency_samples


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=100_000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=5_000)
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args()
    if (
        args.events <= 0
        or args.repeats <= 0
        or args.warmup < 0
        or args.batch_size <= 0
    ):
        parser.error("events/repeats/batch-size must be positive; warmup non-negative")

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
        f"batch_size={args.batch_size} native_available={native_available()}"
    )
    for name, factory in factories.items():
        elapsed, latencies = run(
            factory,
            args.events,
            args.repeats,
            args.warmup,
            args.batch_size,
        )
        median_elapsed = statistics.median(elapsed)
        print(
            f"{name}: median_total={median_elapsed:.6f}s "
            f"throughput={args.events / median_elapsed:.0f} events/s "
            f"amortized_p50={percentile(latencies, 0.50) / 1_000:.2f}us "
            f"amortized_p95={percentile(latencies, 0.95) / 1_000:.2f}us "
            f"amortized_p99={percentile(latencies, 0.99) / 1_000:.2f}us "
            f"amortized_p99.9={percentile(latencies, 0.999) / 1_000:.2f}us"
        )


if __name__ == "__main__":
    main()
