# Compute-Aware Intelligence Scheduling

## Existing capabilities

AITOS already has a Python-first architecture, an optional fixed-point
Rust/PyO3 order-book kernel, candidate rotation/tuning, position monitoring
tiers, Redis Streams, ClickHouse history, and an optional Neo4j semantic
knowledge graph. These are reused rather than replaced by RAG, Kafka, Flink,
or another broker.

This tranche adds a bounded scheduling policy for **derived, replaceable
analysis work only**. It is not a raw market-data queue and must never receive
canonical exchange events, trades, or L3 deltas. Canonical book reconstruction
continues to process every required sequenced update and fails closed on gaps.

## Priority contract

1. POSITION_SAFETY — open-position safety, thesis invalidation and exit work.
2. MARKET_CRITICAL — BTC anchor and critical market-state work.
3. CANDIDATE_ANALYSIS — candidate deep analysis.
4. BACKGROUND_RESEARCH — RAG retrieval, historical backfill, training and
   non-urgent research.

The scheduler applies a bounded pending queue, estimated compute-unit budget,
reserved capacity for position and market-critical work, and optional coalescing
for explicitly replaceable derived tasks. A lower-priority task cannot replace
a higher-priority task with the same coalescing key. At queue capacity, only a
lower-priority replaceable task may be evicted; non-replaceable work is never
silently discarded.

Cost units are admission estimates, not measured CPU time. Calibrate them from
stage histograms before production use. Queue wait, rejected/coalesced/evicted
counts, pending-by-priority, dispatch count and task cost should be exported to
existing data-flow telemetry before wiring the scheduler into scanner or
position runtime.

## Integration and rollout gates

- First run the scheduler in shadow mode against real scanner/position task
  intents and compare dispatch order with current behavior.
- Keep position safety on its existing independent monitor path until shadow
  evidence proves no starvation or latency regression.
- Wire candidate deep analysis only after shadow comparison and explicit tests
  for position priority, cancellation, restart and overload.
- RAG retrieval and historical research stay background-priority and outside
  the live market-data hot path.
- Rust remains opt-in until archived event replay, multi-symbol burst tests,
  Python/Rust parity and read-only shadow parity pass.
- Redis versus Redpanda and Flink/Ray remain benchmark-gated; do not deploy
  additional brokers or distributed runtimes without measured need.
- No strategy thresholds, risk limits, execution behavior, exchange routing,
  or deployment workflows are changed by this module.

## Validation

Run: pytest -q tests/intelligence/test_compute_scheduler.py

This deterministic policy primitive does not certify production throughput,
market-data freshness, or live-trading readiness.
