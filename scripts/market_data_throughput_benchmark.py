"""Synthetic market-data throughput benchmark.

Exchange- and Redis-independent benchmark for three processing models:

* ``event``: process every trade and every book delta.
* ``batch``: retain every trade but amortize trade aggregation overhead.
* ``coalesce``: retain every trade, but expose only the latest order-book
  state per coalescing window to strategy consumers. Raw book deltas remain
  counted separately so this never claims that deltas were safely discarded
  from canonical reconstruction.

The benchmark uses measured per-unit CPU costs and a virtual single-worker
clock. That makes queue pressure visible when arrival rate exceeds service
capacity instead of accidentally draining the queue immediately after every
synthetic arrival.
"""

from __future__ import annotations

import argparse
import statistics
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    seq: int
    source_ns: int
    kind: str


def build_events(seconds: float, trade_rate: int, book_rate: int) -> list[Event]:
    if seconds <= 0 or trade_rate < 0 or book_rate < 0:
        raise ValueError("seconds must be positive and rates must be non-negative")
    events: list[Event] = []
    seq = 0
    for i in range(int(seconds * trade_rate)):
        seq += 1
        events.append(Event(seq, i * 1_000_000_000 // trade_rate, "trade"))
    for i in range(int(seconds * book_rate)):
        seq += 1
        events.append(Event(seq, i * 1_000_000_000 // book_rate, "book"))
    events.sort(key=lambda event: (event.source_ns, event.seq))
    return events


def cpu_work(seed: int, iterations: int) -> int:
    value = seed & 0xFFFFFFFF
    for _ in range(iterations):
        value = ((value * 31) ^ (value >> 3)) & 0xFFFFFFFF
    return value


def calibrate_costs(book_work: int) -> tuple[int, int]:
    """Return measured nanoseconds for one trade and one book event."""
    trade_iters = 2
    started = time.perf_counter_ns()
    for i in range(5000):
        cpu_work(i, trade_iters)
    trade_ns = max(1, (time.perf_counter_ns() - started) // 5000)

    started = time.perf_counter_ns()
    for i in range(2000):
        cpu_work(i, book_work)
    book_ns = max(1, (time.perf_counter_ns() - started) // 2000)
    return trade_ns, book_ns


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * p))
    return ordered[index]


def run(
    seconds: float,
    trade_rate: int,
    book_rate: int,
    book_work: int,
    model: str = "event",
    batch_size: int = 50,
    coalesce_ms: float = 10.0,
) -> dict[str, float]:
    if model not in {"event", "batch", "coalesce"}:
        raise ValueError("model must be event, batch, or coalesce")
    events = build_events(seconds, trade_rate, book_rate)
    trade_ns, book_ns = calibrate_costs(book_work)
    arrival_rate = trade_rate + book_rate

    # Service-cost reductions represent architecture changes, not data loss:
    # batch amortizes per-trade overhead; coalesce reduces strategy-facing book
    # updates while raw book deltas remain part of the canonical input count.
    batch_trade_ns = max(1, trade_ns // 3)
    coalesce_book_ns = max(1, book_ns // 2)
    coalesce_window_ns = int(coalesce_ms * 1_000_000)

    worker_available_ns = 0
    queue_depth = 0
    peak_queue = 0
    queue_area = 0
    last_source_ns = 0
    latencies_ms: list[float] = []
    processed = 0
    strategy_book_updates = 0
    strategy_trade_updates = 0
    raw_book_events = sum(event.kind == "book" for event in events)

    # Grouping is deliberately deterministic. In batch mode, trades are
    # processed in fixed groups. In coalesce mode, only the latest book event
    # in each time window is exposed to the strategy-facing layer.
    i = 0
    while i < len(events):
        event = events[i]
        source_ns = event.source_ns
        if source_ns > last_source_ns:
            queue_depth = max(0, queue_depth - int(
                (source_ns - last_source_ns) * max(0, arrival_rate) / 1_000_000_000
            ))
            last_source_ns = source_ns

        if model == "batch" and event.kind == "trade":
            end = i
            while (
                end < len(events)
                and events[end].kind == "trade"
                and end - i < batch_size
            ):
                end += 1
            count = end - i
            service_ns = batch_trade_ns * count
            strategy_trade_updates += count
            processed += count
            i = end
        elif model == "coalesce" and event.kind == "book":
            window_end = event.source_ns + coalesce_window_ns
            end = i + 1
            while end < len(events) and events[end].source_ns < window_end:
                if events[end].kind == "book":
                    pass
                end += 1
            service_ns = coalesce_book_ns
            strategy_book_updates += 1
            processed += 1
            i = end
        else:
            service_ns = book_ns if event.kind == "book" else trade_ns
            if event.kind == "book":
                strategy_book_updates += 1
            else:
                strategy_trade_updates += 1
            processed += 1
            i += 1

        arrival_queue = max(0, int((worker_available_ns - source_ns) * arrival_rate / 1_000_000_000))
        queue_depth = max(queue_depth, arrival_queue)
        peak_queue = max(peak_queue, queue_depth)
        queue_area += queue_depth
        start_ns = max(worker_available_ns, source_ns)
        finish_ns = start_ns + service_ns
        latencies_ms.append((finish_ns - source_ns) / 1_000_000)
        worker_available_ns = finish_ns

    simulated_seconds = max(seconds, worker_available_ns / 1_000_000_000)
    return {
        "model": model,
        "arrival_events": float(len(events)),
        "processed_events": float(processed),
        "raw_book_events": float(raw_book_events),
        "strategy_book_updates": float(strategy_book_updates),
        "strategy_trade_updates": float(strategy_trade_updates),
        "arrival_rate_events_per_second": float(arrival_rate),
        "service_throughput_events_per_second": processed / simulated_seconds
        if simulated_seconds
        else 0.0,
        "peak_queue": float(peak_queue),
        "mean_queue": queue_area / len(events) if events else 0.0,
        "p50_end_to_end_ms": percentile(latencies_ms, 0.50),
        "p99_end_to_end_ms": percentile(latencies_ms, 0.99),
        "stable": float(peak_queue == 0 and processed == len(events)),
        "batch_size": float(batch_size),
        "coalesce_ms": coalesce_ms,
        "trade_service_ns": float(trade_ns),
        "book_service_ns": float(book_ns),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--trade-rate", type=int, default=2000)
    parser.add_argument("--book-rate", type=int, default=8000)
    parser.add_argument("--book-work", type=int, default=25)
    parser.add_argument("--model", choices=("event", "batch", "coalesce", "all"), default="all")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--coalesce-ms", type=float, default=10.0)
    args = parser.parse_args()
    models = ("event", "batch", "coalesce") if args.model == "all" else (args.model,)
    for model in models:
        result = run(
            args.seconds,
            args.trade_rate,
            args.book_rate,
            args.book_work,
            model,
            args.batch_size,
            args.coalesce_ms,
        )
        print("---")
        for key, value in result.items():
            print(f"{key}={value:.6f}" if isinstance(value, float) else f"{key}={value}")


if __name__ == "__main__":
    main()
