# Market-data throughput benchmark

This benchmark is intentionally synthetic and exchange/Redis independent. It answers one question before production tuning: **can the application-side processing model keep up with a sustained event rate?**

The benchmark models three execution strategies:

- `event`: process every trade and every order-book delta.
- `batch`: retain every trade, but amortize trade aggregation overhead.
- `coalesce`: retain every trade and keep raw book events counted, while exposing only the latest book state per coalescing window to the strategy-facing layer.

Recommended scenarios:

```bash
# Baseline
python scripts/market_data_throughput_benchmark.py --trade-rate 2000 --book-rate 8000 --book-work 25 --model all

# Higher sustained rate
python scripts/market_data_throughput_benchmark.py --trade-rate 4000 --book-rate 16000 --book-work 50 --model all

# Deliberate saturation test: this is expected to expose queue growth in event mode.
python scripts/market_data_throughput_benchmark.py --trade-rate 4000 --book-rate 16000 --book-work 250 --model all
```

Interpretation:

- `arrival_rate_events_per_second` is the synthetic ingress rate.
- `service_throughput_events_per_second` must exceed ingress for a stable single worker.
- `peak_queue` and p99 end-to-end latency should remain bounded for a healthy model.
- `batch` must retain every trade event; it is an efficiency test, not a loss policy.
- `coalesce` is only a strategy-facing optimization. It must **not** be used to skip required canonical order-book deltas when reconstructing the exchange book.
- Do not increase Redis CPU/pool/concurrency based on this benchmark alone; it isolates application processing from Redis/network effects.

The benchmark is intentionally not a production-capacity claim. The next decision must compare these results with telemetry from the real AITOS process: event-loop lag, actual queue depth, CPU, Redis stream lag, and end-to-end source-to-strategy latency.
