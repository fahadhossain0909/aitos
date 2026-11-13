# AITOS World Intelligence — Implementation Foundation

## Status

This change introduces the first executable, dependency-light foundation. It does not claim all provider integrations, predictive models, or trading-policy wiring are complete.

## Safety boundary

- External intelligence is advisory evidence, never an order source.
- Unverified, single-source, disputed, retracted, stale, future-dated, or invalid observations cannot create directional impact.
- Missing asset/event beta defaults to zero; cross-asset relationships are never invented.
- Impact values are bounded research features, not calibrated return forecasts.
- The governor emits recommendations only. It cannot mutate core risk limits, sizing, stops, execution, or credentials.
- Intelligence outages leave the existing market-data and trading path unchanged.

## Contracts

SourceEvidence captures source provenance, source reliability, primary-source status, publication time, and first-seen time. IntelligenceEvent provides a versioned canonical record with bounded sentiment, severity, novelty, and confidence. The deduplication key identifies source-story duplicates; it does not imply syndicated copies are independent confirmations.

## Impact model

estimate_asset_impact applies signed sentiment, severity, novelty, model confidence, source reliability, verification status, a supplied point-in-time asset beta, and exponential half-life decay. The default beta is zero. Production betas must be estimated from point-in-time event studies and validated out of sample before use.

## Implemented durable ingestion slice

- A point-in-time asset snapshot aggregates verified event evidence, beta estimates, attention, horizon-specific impact features and advisory risk assessment. It filters both event observations and beta estimates by the snapshot as-of timestamp.
- A point-in-time event-study beta estimator filters by asset/event/horizon and requires the forward outcome to have become observable before the as-of timestamp. It defaults to zero below the minimum sample count, shrinks estimates toward zero, and bounds output.
- A source-verification gate uses operator-maintained publisher groups, freshness, reliability and explicit primary-source metadata. Multiple feeds from the same publisher count once; disputed/retracted observations fail closed. This is provenance verification, not independent factual adjudication.
- An explainable English financial-headline lexicon emits bounded sentiment, coarse event type/severity, matched terms, and a deliberately capped heuristic confidence (0.55). It is annotation-only, does not verify claims, and is not a multilingual trained NLP model.
- A bounded RSS/Atom adapter supports conditional ETag/Last-Modified requests, HTTPS host allowlists (including redirects), response-size limits, and DTD/entity rejection. It emits neutral, unverified observations; it does not infer sentiment or facts.
- ClickHouse event evidence is stored append-only in `world_intelligence_events`, keyed by source plus deterministic source-story identity.
- Per-source opaque adapter cursors are stored in `world_intelligence_cursors`.
- Durable ingestion order is **append ClickHouse evidence → publish normalized Redis event → save cursor**. A failed insert or publish leaves the cursor unchanged, so the source page is replayed rather than lost.
- This is at-least-once delivery, not a distributed transaction. A single active poller per source is required; consumers must use the deterministic deduplication key. ReplacingMergeTree deduplication is eventual, so exact reads should use `FINAL` or an aggregation strategy.
- The store deliberately does not create a Redis consumer group or alter existing EventBus ACK/retry/DLQ policy. The runtime worker and production ClickHouse integration still need wiring and service-level verification.

## Integration plan

1. Add source adapters for official SEC, Federal Reserve/FRED, GDELT, exchange announcements, and licensed news/social APIs.
2. Add raw-response retention, source-specific durable cursors, and stronger operational idempotency/recovery around the normalized ClickHouse event store.
3. Publish normalized events through the existing Redis Streams EventBus and its established ACK/retry/DLQ semantics.
4. Add source verification, entity resolution, semantic clustering, multilingual financial NLP, bot/spam detection, and narrative tracking.
5. Expand the point-in-time event-study baseline with event-specific calibration, robust uncertainty, multiple horizons, and chronological OOS validation before any model use.
6. Attach compact, timestamped intelligence snapshots to candidate/decision evidence and the existing DecisionOutcomeLedger.
7. Run shadow-only replay, leakage checks, ablation, and chronological walk-forward tests before advisory paper policy.
8. Require explicit review and champion/challenger governance before any decision influence.

## Not included in this foundation

No provider credentials, scraping bypass, paid API dependency, LLM runtime, scheduled external network worker, ClickHouse consumer worker, Redis consumer group, scanner/risk wiring, autonomous strategy mutation, paper/live policy change, or automatic model promotion is introduced here. The RSS adapter is an opt-in library component and must be configured with an operator-approved feed URL and hostname allowlist. Those require provider-specific contracts, integration tests, and runtime evidence.
