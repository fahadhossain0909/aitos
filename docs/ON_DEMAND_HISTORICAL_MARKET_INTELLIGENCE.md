# On-Demand Historical Market Intelligence

## Status

The first implementation is integrated into the staged scanner on
`arena/ed127d35-aitos`. It is an evidence-only enrichment path. It does not
change scanner ranking, entry/exit rules, sizing, leverage, risk limits, or
execution behavior.

## Workload model

1. The existing universe scanner continues to perform its bounded lightweight
   market scan and promotion stages.
2. The expensive scanner evaluates the existing Top-10 cohort.
3. Only the final globally ranked Top-2 candidates receive deep historical
   retrieval. This is wired into both `scan_all()` and the paper/runtime
   `iter_candidate_batches()` path.
4. Historical requests are asynchronous and bounded. The scanner's decision
   bridge no longer makes an extra historical REST request for every symbol.
5. Retrieved candles and derived context are attached to the promoted
   candidate and passed into the existing AI decision context.

The Top-2 enrichment path buffers the finite candidate batches until the
global ranking is known. This avoids allowing a candidate to reach decision
evaluation before the global promotion boundary is established.

## Resource controls

`OnDemandHistoricalIntelligence` provides:

- TTL/LRU cache, bounded by both entry count and total candle count;
- in-flight request coalescing for identical symbol/timeframe/limit keys;
- bounded concurrent fetches;
- a rolling 60-second weighted request budget using the USD-M Futures kline
  request-weight schedule;
- a per-request candle cap (maximum 1,500, configured default 1,000);
- cache-miss and budget/fetch metrics;
- best-effort failure behavior: missing history is marked unavailable and
  does not block execution.

The initial scanner integration uses a 15-minute TTL, at most 24 cache entries,
12,000 cached candles, two concurrent deep fetches, and a 60-weight/minute
budget per service instance. These are conservative starting values, not
production-calibrated limits.

## Historical evidence

The service computes a bounded recent-return/realized-volatility summary and
uses the existing historical-analogue engine to retrieve prior similar paths
and their subsequent outcomes. The payload includes sample count, analogue
indices/similarity, directional outcome frequencies, median/best/worst
forward return, and an explicit evidence-only/look-ahead policy marker.

Historical outcome frequencies are empirical sample summaries, not calibrated
probabilities or forecasts. The AI layer may contextualize them but must not
treat them as guaranteed direction or override contradictory live evidence.

## Storage policy

- No full candle archive is added for the entire symbol universe.
- Deep candle cache is process-local, TTL-bounded, and evicted by LRU/total
  candle count.
- Redis remains bounded hot state/transport; ClickHouse remains the canonical
  durable historical source where the existing pipeline has persisted data.
- This implementation does not create a new durable HMIE evidence table or
  retain raw candles for rejected candidates.

## Current limitations / certification gates

- The request-weight budget and cache are process-local. A multi-replica
  deployment needs a shared Redis/central rate-budget adapter before the
  configured value can be treated as a global exchange-IP budget.
- Binance REST fallback behavior and exchange-specific request weights must be
  verified for each adapter/market before multi-venue rollout.
- The HMIE evidence payload is passed to the existing decision context, but a
  dedicated durable evidence snapshot/retention and Decision-to-Outcome
  attribution contract is not yet certified.
- TTL, cache size, deep-candidate count, request weight, and memory use need
  workload benchmarks against the target Render hybrid runtime.
- Unit tests must pass in CI, followed by replay/ablation and walk-forward /
  locked-holdout validation. No production or live-trading certification is
  implied.

## Focused tests

```bash
pytest -q tests/intelligence/test_historical_market_intelligence.py
pytest -q tests/test_staged_scanner.py tests/test_clickhouse_replay_loader.py
pytest -q tests/intelligence/test_historical_analogue.py
```
