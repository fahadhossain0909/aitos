# Market-data throughput benchmark

This benchmark is intentionally synthetic and exchange/Redis independent. It answers one question before production tuning: **can the application-side processing model keep up with a sustained event rate?**

Recommended scenarios:

```bash
python scripts/market_data_throughput_benchmark.py --trade-rate 2000 --book-rate 8000 --book-work 25
python scripts/market_data_throughput_benchmark.py --trade-rate 4000 --book-rate 16000 --book-work 25
python scripts/market_data_throughput_benchmark.py --trade-rate 4000 --book-rate 16000 --book-work 50
```

Interpretation:

- `throughput_events_per_second` must exceed the synthetic arrival rate for a stable queue.
- `peak_queue` should remain bounded rather than grow with test duration.
- Compare the same scenarios before/after batching or order-book state coalescing.
- Do not increase Redis CPU/pool/concurrency based on this benchmark alone; it is specifically designed to isolate application processing capacity from Redis/network effects.

The next benchmark revision should add three execution models: current per-event processing, batched trade aggregation, and sequence-aware order-book coalescing. The result should compare throughput, queue growth, p50/p99 latency, CPU, and trade-loss/sequence-integrity invariants.
