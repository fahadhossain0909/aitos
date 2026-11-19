"""Bounded-queue burst benchmark for the optional native order-book core.

This isolates producer backpressure and queue residence time. It is not a
network, Binance, scheduler-jitter, or end-to-end market-data benchmark.
"""

import argparse
import asyncio
import math
import statistics
import time

from aitos.native_core import PythonFixedPointOrderBook, create_order_book, native_available


def percentile(samples, quantile):
    ordered = sorted(samples)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


async def run_once(factory, events, queue_capacity, batch_size, burst_size):
    queue = asyncio.Queue(maxsize=queue_capacity)
    latencies_ns = []
    producer_blocked_ns = 0
    producer_blocked_puts = 0
    max_backlog = 0
    produced = 0
    applied = 0
    book = factory()

    async def produce():
        nonlocal producer_blocked_ns, producer_blocked_puts, max_backlog, produced
        for start in range(0, events, burst_size):
            stop = min(start + burst_size, events)
            for index in range(start, stop):
                update_id = index + 11
                update = (
                    update_id,
                    update_id,
                    [(100_000 + index % 100, 1 + index % 7)],
                    [(100_200 + index % 100, 1 + index % 5)],
                    update_id - 1 if index else None,
                )
                before = time.perf_counter_ns()
                await queue.put((update, time.perf_counter_ns()))
                blocked = time.perf_counter_ns() - before
                if blocked > 50_000:
                    producer_blocked_puts += 1
                    producer_blocked_ns += blocked
                produced += 1
                max_backlog = max(max_backlog, queue.qsize())
            # Yield at burst boundaries so the consumer can make progress.
            await asyncio.sleep(0)

    producer = asyncio.create_task(produce())
    started = time.perf_counter()
    while not producer.done() or not queue.empty():
        first = await queue.get()
        batch = [first]
        while len(batch) < batch_size:
            try:
                batch.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        now = time.perf_counter_ns()
        latencies_ns.extend(now - enqueued_ns for _, enqueued_ns in batch)
        count = book.apply_deltas([update for update, _ in batch])
        if count != len(batch):
            raise RuntimeError(f"applied {count} of {len(batch)} queued events")
        applied += count
        for _ in batch:
            queue.task_done()
        await asyncio.sleep(0)

    await producer
    await queue.join()
    elapsed = time.perf_counter() - started
    if produced != events or applied != events:
        raise RuntimeError(f"event loss: produced={produced}, applied={applied}")
    return {
        "elapsed": elapsed,
        "throughput": applied / elapsed,
        "queue_wait_ns": latencies_ns,
        "producer_blocked_ns": producer_blocked_ns,
        "producer_blocked_puts": producer_blocked_puts,
        "max_backlog": max_backlog,
    }


async def benchmark(factory, args):
    samples = [
        await run_once(
            factory,
            args.events,
            args.queue_capacity,
            args.batch_size,
            args.burst_size,
        )
        for _ in range(args.repeats)
    ]
    waits = [value for sample in samples for value in sample["queue_wait_ns"]]
    median_elapsed = statistics.median(sample["elapsed"] for sample in samples)
    blocked_ns = sum(sample["producer_blocked_ns"] for sample in samples)
    blocked_puts = sum(sample["producer_blocked_puts"] for sample in samples)
    max_backlog = max(sample["max_backlog"] for sample in samples)
    print(
        f"median_throughput={args.events / median_elapsed:.0f} events/s "
        f"queue_wait_p50={percentile(waits, 0.50) / 1_000:.2f}us "
        f"queue_wait_p95={percentile(waits, 0.95) / 1_000:.2f}us "
        f"queue_wait_p99={percentile(waits, 0.99) / 1_000:.2f}us "
        f"queue_wait_p99.9={percentile(waits, 0.999) / 1_000:.2f}us "
        f"producer_blocked_puts={blocked_puts} "
        f"producer_blocked_total={blocked_ns / 1_000_000:.2f}ms "
        f"max_backlog={max_backlog}/{args.queue_capacity}"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=50_000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--queue-capacity", type=int, default=256)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--burst-size", type=int, default=256)
    args = parser.parse_args()
    if min(args.events, args.repeats, args.queue_capacity, args.batch_size, args.burst_size) <= 0:
        parser.error("all arguments must be positive")

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
        f"queue_capacity={args.queue_capacity} batch_size={args.batch_size} "
        f"burst_size={args.burst_size} native_available={native_available()}"
    )
    for name, factory in factories.items():
        print(f"{name}: ", end="", flush=True)
        asyncio.run(benchmark(factory, args))


if __name__ == "__main__":
    main()
