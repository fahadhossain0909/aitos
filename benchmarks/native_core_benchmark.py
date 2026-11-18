"""Reproducible local microbenchmark; not an end-to-end latency claim."""

import argparse
import statistics
import time

from aitos.native_core import (
    PythonFixedPointOrderBook,
    create_order_book,
    native_available,
)


def run(factory, events, repeats):
    samples = []
    for _ in range(repeats):
        book = factory()
        started = time.perf_counter()
        for index in range(events):
            update_id = index + 11
            book.apply_delta(
                update_id,
                update_id,
                bids=[(100_000 + index % 100, 1 + index % 7)],
                asks=[(100_200 + index % 100, 1 + index % 5)],
                previous_update_id=update_id - 1 if index else None,
            )
        samples.append(time.perf_counter() - started)
    return samples


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=100_000)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.events <= 0 or args.repeats <= 0:
        parser.error("events and repeats must be positive")

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
        f"events={args.events} repeats={args.repeats} "
        f"native_available={native_available()}"
    )
    for name, factory in factories.items():
        samples = run(factory, args.events, args.repeats)
        median = statistics.median(samples)
        print(
            f"{name}: median={median:.6f}s "
            f"events_per_second={args.events / median:.0f} "
            f"min={min(samples):.6f}s max={max(samples):.6f}s"
        )


if __name__ == "__main__":
    main()
