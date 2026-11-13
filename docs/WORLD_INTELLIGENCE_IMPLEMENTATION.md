# AITOS World Intelligence — Implementation Foundation

## Status

This change introduces the first executable, dependency-light foundation. It does not claim all provider integrations, predictive models, or trading-policy wiring are complete.

## Safety boundary

- External intelligence is advisory evidence, never an order source.
- Unverified, disputed, retracted, stale, future-dated, or invalid observations cannot create directional impact.
- Missing asset/event beta defaults to zero; cross-asset relationships are never invented.
- Impact values are bounded research features, not calibrated return forecasts.
- The governor emits recommendations only. It cannot mutate core risk limits, sizing, stops, execution, or credentials.
- Intelligence outages leave the existing market-data and trading path unchanged.

## Contracts

SourceEvidence captures source provenance, source reliability, primary-source status, publication time, and first-seen time. IntelligenceEvent provides a versioned canonical record with bounded sentiment, severity, novelty, and confidence. The deduplication key identifies source-story duplicates; it does not imply syndicated copies are independent confirmations.

## Impact model

estimate_asset_impact applies signed sentiment, severity, novelty, model confidence, source reliability, verification status, a supplied point-in-time asset beta, and exponential half-life decay. The default beta is zero. Production betas must be estimated from point-in-time event studies and validated out of sample before use.

## Integration plan

1. Add source adapters for official SEC, Federal Reserve/FRED, GDELT, exchange announcements, and licensed news/social APIs.
2. Persist raw observations and normalized events with provenance in ClickHouse; store operational cursors and idempotency state durably.
3. Publish normalized events through the existing Redis Streams EventBus and its established ACK/retry/DLQ semantics.
4. Add source verification, entity resolution, semantic clustering, multilingual financial NLP, bot/spam detection, and narrative tracking.
5. Estimate asset-specific impact with historical point-in-time event studies and calibrated uncertainty.
6. Attach compact, timestamped intelligence snapshots to candidate/decision evidence and the existing DecisionOutcomeLedger.
7. Run shadow-only replay, leakage checks, ablation, and chronological walk-forward tests before advisory paper policy.
8. Require explicit review and champion/challenger governance before any decision influence.

## Not included in this foundation

No provider credentials, scraping bypass, paid API dependency, LLM runtime, external network worker, ClickHouse migration, Redis consumer, scanner/risk wiring, autonomous strategy mutation, paper/live policy change, or automatic model promotion is introduced here. Those require provider-specific contracts, integration tests, and runtime evidence.
