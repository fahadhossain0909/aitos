# Live Market Freshness Policy

AITOS is a live-trading system first. Historical collection is a secondary workload and must never reduce live-market freshness, liveness, or robustness.

## Hard invariants

1. The live market-data path must never wait for ClickHouse, historical persistence, or a replay backlog.
2. When live handoff is under pressure, prefer the newest market event over an older queued event.
3. A live event that has waited beyond the configured queue-age budget may be dropped rather than published stale.
4. A failed downstream publish must not be requeued indefinitely. Requeueing an old event can create a stale backlog and starve fresh market data.
5. Historical persistence is best-effort. Queue overflow may reject historical work without backpressuring the live producer.
6. High-volume historical trade collection is scoped to BTCUSDT and LTCUSDT for the current architecture. Additional historical collection belongs in a separate or independently throttled layer.
7. Live order-book depth should prefer the highest practical depth. The default target is 1000 levels, with a configurable lower fallback (100 levels by default) when sustained live-path pressure demonstrates that the current compute budget cannot sustain the preferred depth.
8. Deep historical order-book collection must not open duplicate live sockets in the trading process by default. It is opt-in through `AITOS_ENABLE_DEEP_HISTORY` and should eventually be moved to a separate historical worker/process.

## Why dropping is acceptable

For live trading, a fresh observation is more valuable than a complete but delayed history. Some high-volume trades, deep order-book updates, and historical records can be lost under resource pressure. What must not happen is allowing those records to accumulate until the strategy consumes stale market state.

## Operational interpretation

The primary health questions are:

- Is the newest WebSocket event arriving fresh?
- Is the live gateway able to hand off the newest event without waiting?
- Is the downstream bus responsive within the publish timeout?
- Is the live queue staying within its age budget?
- Is order-book depth sustainable at the preferred level?

ClickHouse throughput, historical completeness, and deep-history queue depth are secondary metrics and must not be used as reasons to slow the live path.
