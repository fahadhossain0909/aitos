# AITOS On-Demand Historical Intelligence — Operational Design

## Market-wide discovery

The Binance USDⓈ-M all-market 24-hour ticker stream feeds compact, short-lived
universe state. The state contains only last price, 24-hour change, quote
volume, trade count, range and spread. It is not a candle archive and is not
used as a directional signal. Candidate ranking uses this low-cost context;
the expensive scanner still performs its existing per-candidate checks.

Binance's all-market ticker array emits only symbols that changed. The live
context therefore retains last-known ticker observations with a bounded
freshness window. Symbols without sufficiently fresh observations are omitted
rather than ranked using stale data. The stream is used for discovery only;
execution and risk still require their existing live feeds.

## Historical retrieval

Only the globally promoted Top-2 candidates are eligible for deep historical
retrieval. Each promoted candidate receives independent evidence for its active
scanner interval plus the configured 5m, 15m, 1h and 4h intervals (duplicate
intervals are collapsed). The active interval remains the primary context;
other intervals are retained as separate evidence and are never blended into
a synthetic directional score. Binance's case-sensitive 1M monthly interval
is kept distinct from 1m minute candles. Retrieval is bounded by:
- process-local TTL/LRU limits on entry count and total candles;
- Redis shared cache and short-lived distributed request lock;
- atomic Redis sliding-window weighted request budget;
- maximum request size of 1,000 candles by default (hard cap 1,500);
- maximum two concurrent deep retrievals.

If Redis is configured but unavailable, the shared-budget reservation fails
closed and HMIE evidence is marked unavailable. The candidate pipeline
continues without historical enrichment. Historical context never blocks or
changes the execution/risk decision.

## Durable evidence

A selected candidate's evidence snapshot (including unavailable status) is
stored in ClickHouse with a candidate ID, symbol, timeframe, capture time,
schema version and deterministic evidence ID. Raw historical candles are not
persisted by this feature. The existing DecisionOutcomeLedger remains the
decision/label system of record; candidate IDs link the evidence snapshot to
the decision lineage.

## Statistical validity

Historical analogue outcomes are measured after the matched pattern window.
The matcher excludes any historical analogue whose complete forward horizon
overlaps the current pattern. The walk-forward evaluator uses prefix-only
predictions and future labels, non-overlapping label intervals, Brier score,
baseline Brier, Brier skill, log loss and directional accuracy. It reports
metrics only; it does not calibrate, promote, or change any live model.

## Operational limits

The shared request budget is global to Redis key scope and is initially set
to 60 Binance request-weight units per rolling minute. Tune it to the exchange
IP's complete workload, not HMIE alone. Cache TTL and freshness thresholds
must be load-tested against the Render hybrid topology. No production rollout
or live-trading certification is implied.
