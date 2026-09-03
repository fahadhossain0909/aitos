# Redis consumer concurrency

`REDIS_CONSUMER_CONCURRENCY` controls bounded application-side concurrency for Redis Stream consumers. The initial production value is 8, with a hard ceiling of 32.

This is intentionally separate from the Redis connection pool (`REDIS_MAX_CONNECTIONS`, currently 256). Increasing the pool does not make a single sequential event handler concurrent.

Before increasing the value, compare handler throughput, latency, pending-entry growth, and downstream database pressure. Market-data handlers must preserve their state-ordering guarantees; concurrency should not allow an older event to overwrite newer state.
