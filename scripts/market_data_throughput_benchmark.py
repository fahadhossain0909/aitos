"""Synthetic market-data throughput benchmark.

This benchmark deliberately does not connect to an exchange or Redis. It measures
whether the CPU-bound normalization/aggregation model can keep up with configurable
trade and order-book event rates, and reports queue growth and processing latency.
"""

from __future__ import annotations

import argparse
import heapq
import statistics
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    seq: int
    source_ns: int
    kind: str
    updates: int = 1


def build_events(seconds: float, trade_rate: int, book_rate: int) -> list[Event]:
    total = int(seconds * (trade_rate + book_rate))
    events: list[Event] = []
    seq = 0
    for i in range(int(seconds * trade_rate)):
        seq += 1
        events.append(Event(seq, i * 1_000_000_000 // trade_rate, "trade"))
    for i in range(int(seconds * book_rate)):
        seq += 1
        events.append(Event(seq, i * 1_000_000_000 // book_rate, "book"))
    events.sort(key=lambda event: event.source_ns)
    return events[:total]


def process_event(event: Event, book_work: int) -> int:
    value = event.seq & 0xFFFF
    if event.kind == "book":
        for _ in range(book_work):
            value = ((value * 31) ^ (value >> 3)) & 0xFFFFFFFF
    else:
        value = ((value * 17) ^ (value >> 2)) & 0xFFFFFFFF
    return value


def run(
    seconds: float, trade_rate: int, book_rate: int, book_work: int
) -> dict[str, float]:
    events = build_events(seconds, trade_rate, book_rate)
    queue: list[Event] = []
    latencies: list[float] = []
    peak = 0
    processed = 0
    started = time.perf_counter_ns()
    for event in events:
        heapq.heappush(queue, (event.source_ns, event))
        peak = max(peak, len(queue))
        _, current = heapq.heappop(queue)
        process_event(current, book_work)
        processed += 1
        latencies.append((time.perf_counter_ns() - started) / 1_000_000)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
    return {
        "events": float(len(events)),
        "processed": float(processed),
        "elapsed_seconds": elapsed,
        "throughput_events_per_second": processed / elapsed if elapsed else 0.0,
        "peak_queue": float(peak),
        "p50_processing_wall_ms": statistics.median(latencies) if latencies else 0.0,
        "p99_processing_wall_ms": (
            statistics.quantiles(latencies, n=100)[98]
            if len(latencies) >= 100
            else (max(latencies) if latencies else 0.0)
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--trade-rate", type=int, default=2000)
    parser.add_argument("--book-rate", type=int, default=8000)
    parser.add_argument("--book-work", type=int, default=25)
    args = parser.parse_args()
    result = run(args.seconds, args.trade_rate, args.book_rate, args.book_work)
    for key, value in result.items():
        print(f"{key}={value:.6f}")


if __name__ == "__main__":
    main()
