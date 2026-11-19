# AEP-1 Phase 3 — Runtime integration and candidate-scoped verification

Date: 2026-11-19

## Implemented in this tranche

### 1. Opt-in shadow-consumer lifecycle

- `DataIngestionService.market_data_bus` exposes the canonical MarketDataBus without exposing the runtime's private fields.
- `build_system(..., market_shadow_consumer=...)` accepts an explicit read-only consumer dependency.
- `initialize_all()` starts it only after canonical market-data ingestion and lifecycle subscriptions are established.
- `shutdown_all()` drains and stops it before tearing down modules.
- Lightweight legacy component containers remain compatible; missing optional shadow-consumer attributes are treated as disabled.
- Startup fails closed if a consumer was explicitly supplied but canonical market-data bus is unavailable.
- The default application still creates no shadow consumer. No new exchange subscriptions, scanner score changes, execution authority or production setting are enabled by this wiring.

### 2. Candidate identity is part of scanner health

The scanner now reports `last_candidate_symbols` alongside `last_candidate_count`. The staged scanner records the actual promoted Top-2 symbols, rather than treating a pre-ranking batch count as the promoted candidate set. Bounded batch scanning accumulates candidate identities across the current scan cycle.

This makes subscription coverage auditable by instrument identity. A candidate count is not evidence that a non-BTC candidate exists.

### 3. Paper canary coverage is candidate-scoped

The paper canary now checks that each actual promoted non-BTC candidate has both live kline and deep order-book coverage. A BTC-only candidate set no longer creates a false failure merely because BTC is the anchor. If the scanner reports a nonzero candidate count but omits candidate identities, the canary fails closed.

This changes only the correctness of the verification assertion; it does not lower data-quality requirements for any candidate that was actually promoted.

### 4. Pending-order risk snapshot contract

`LivePortfolioTracker` and `PersistentLivePortfolioTracker` accept an optional asynchronous authoritative pending-order snapshot provider. A configured provider must return a complete order inventory plus coherent fresh mark, protective stop and leverage mappings. The existing pure Binance USD-M projection applies remaining quantity after partial fills and rejects stale, missing, ambiguous or invalid evidence.

Each refresh invalidates the prior snapshot before fetching new evidence. A failed refresh prevents construction of a live portfolio risk snapshot. No provider is enabled by default. The live runner's independent full-account reconciliation continues to block unpriced pending entry orders; a fabricated stop or leverage value is never substituted.

### 5. Timestamp-aligned cross-market diagnostics

The generic market engine now exposes UTC epoch-bucket return alignment and delayed cross-correlation with strict finite/typed parameter validation. Missing adjacent buckets are not bridged. This is a diagnostic correlation, not proof of causality; the dedicated Futures/Spot analyzer remains the required diagnostic for that market pair.

## Verification status

The previous CI head exposed three regressions: optional lifecycle attribute access, an epoch-bucket test expecting relative indexes, and an over-broad canary assumption that every nonempty candidate count implied a non-BTC candidate. Code and assertions have been corrected.

The latest workflow head must complete before this addendum is marked CI-verified. Unit, integration, Redis, ClickHouse/PostgreSQL, paper-canary and exchange evidence are reported separately. A green unit suite is not a substitute for testnet account reconciliation or a sustained paper soak.

## Remaining hard gates

- No authenticated account-backed stop-risk provider is wired into the live portfolio tracker. Open entry orders without trusted stop/mark/leverage evidence remain a fail-closed condition.
- The shadow consumer is opt-in through the composition root; the standard paper/live entrypoints do not instantiate it by default.
- The protective intent journal records a clientAlgoId preclaim before conditional-order POST and records accepted protection after response. Ambiguous POST-to-ack restart remains quarantined and needs explicit inventory/child-fill/position reconciliation; no automatic repair is authorized.
- SQLite execution intent and ClickHouse analytics/history are ordered cross-store writes, not one atomic financial transaction. Crash/outage reconciliation and restore proof remain required.
- Full venue-calibrated queue position, full-depth impact, fee/funding schedules, and production latency percentiles require captured historical/testnet evidence.
- Model promotion, live decision influence, production deployment and risk-limit changes remain disabled.

## Safety boundary

This work does not merge the Draft PR, deploy services, enable live orders, change risk limits, clear execution journals, or promote a model.
