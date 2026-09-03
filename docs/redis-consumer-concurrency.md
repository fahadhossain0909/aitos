# Redis consumer concurrency

`REDIS_CONSUMER_CONCURRENCY` controls bounded application-side concurrency for Redis Stream consumers. The production default is 8, with a hard ceiling of 32.

This is intentionally separate from the Redis connection pool (`REDIS_MAX_CONNECTIONS`, currently 256). Increasing the pool does not make a single sequential event handler concurrent.

## Ordering model

The EventBus now uses **one ordered worker per discovered Redis Stream**. Workers for different streams may process concurrently, but a worker never processes two entries from the same stream at the same time. This keeps per-symbol market-state ordering intact while allowing independent symbols to make progress in parallel.

Concurrency is bounded by `REDIS_CONSUMER_CONCURRENCY` with a semaphore around handler execution. Pending-message reclamation uses the same bound for durable consumers; live-only subscriptions continue to skip pending reclaim.

## Tuning

Start with the default of 8. Before increasing it, compare handler throughput, latency, pending-entry growth, Redis connection usage, and downstream database pressure. Increasing concurrency is useful only when the downstream handler has independent work to execute; it must not be used to hide an overloaded ClickHouse/Neo4j or other shared bottleneck.

For market data, correctness takes priority over raw throughput: an older event must not be allowed to overwrite newer state merely because it completed later.
