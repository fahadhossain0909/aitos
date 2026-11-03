# AITOS Market Data V1 — Implementation Contract

## Goals

1. One canonical market-data plane for Binance Spot, USD-M Futures, COIN-M Futures and Options.
2. No V2/V3 generation-number topology.
3. Redis is bounded hot state/event transport, never the historical source of truth.
4. ClickHouse is the durable market-data/history store.
5. Neo4j stores relationships and market-structure knowledge, not streaming payloads.
6. REST recovery is explicit degraded mode; it must never masquerade as live WebSocket state.
7. Every stage exposes freshness, queue/backlog, sequence, reconnect and error telemetry.
8. BTC is always deep/live. BTC and LTC retain 100-level historical books (50 bid + 50 ask). Other symbols are promoted dynamically when they become candidates.

## Data flow

```text
Binance product APIs
        |
        v
Market Data Gateway
  - WS lifecycle
  - REST snapshot/recovery
  - sequence validation
  - bounded queues
  - reconnect/backoff
  - rate-limit control
        |
        v
Canonical Event Bus
  market.trade
  market.book.delta
  market.book.snapshot
  market.ticker
  market.funding
  market.open_interest
  market.liquidation
  market.options
  market.instrument
        |
        +------------------+-------------------+
        v                  v                   v
   Hot State           ClickHouse           Neo4j
   (bounded)           (durable)          (relations)
        |
        v
Feature Engine
        |
        v
Adaptive Universe Scanner
 ALL -> Top25 -> Top10 -> Top5 -> Top2
        |
        v
Deep Analysis
 BTC + best 2 non-BTC
        |
        v
Strategy / AI
        |
        v
Execution Router
 Spot / Futures
```

## Live-state invariant

A symbol is `coherent_live` only when all required components satisfy their freshness limits and the order book is sequence-valid. A REST response may populate recovery state, but the resulting state must carry `source=rest` and `degraded=true` unless it has subsequently been confirmed by live WS events.

Required telemetry fields:

- `event_id`
- `venue`
- `market_type`
- `symbol`
- `source`
- `event_time`
- `received_at`
- `processed_at`
- `source_age_ms`
- `queue_age_ms`
- `processing_latency_ms`
- `sequence`
- `sequence_gap_count`
- `reconnect_count`
- `queue_depth`
- `dropped_count`
- `error_count`
- `degraded`

## Backpressure policy

All high-rate queues are bounded. Producers never wait indefinitely for consumers. Each queue has a named capacity and an observable overflow policy. Market data may be coalesced only where semantics permit it (for example, intermediate book updates), never silently dropped for trades or sequence-critical deltas.

A consumer that cannot keep up must expose `queue_depth`, `oldest_event_age_ms`, `processing_rate`, `input_rate` and `drop/coalesce counters`. This makes accumulation immediately diagnosable.

## Subscription tiers

### Tier 0 — Universe

Cheap market-wide data used for ranking: ticker, volume, volatility, funding/OI where available, spread/liquidity proxies, and instrument metadata.

### Tier 1 — Candidate

Top 25/10/5 candidates receive trades and required book data for increasingly expensive features.

### Tier 2 — Deep

BTC is permanent. Current Top-2 non-BTC candidates receive full deep analysis. LTC receives persistent 100-level historical book storage but is not automatically forced into every expensive calculation.

### Options

Options market data is normalized into IV, skew, term structure, OI, volume, expiry concentration and Greeks where available. Raw high-frequency options data is not pushed through the same hot path as futures trades.

## Storage policy

### Redis

- Current state only.
- Bounded streams/queues.
- Explicit TTL/retention.
- No long-lived historical archive.
- No repeated XGROUP CREATE on every consumer start.
- Consumer identity is stable and created idempotently once.

### ClickHouse

Durable history for trades, selected market metrics, derived features, scanner decisions, strategy decisions and execution records. BTC/LTC order-book history uses 100 levels. Non-anchor full-depth history is subscription-driven and retention-limited.

### Neo4j

Instrument relationships, spot/futures relationships, lead-lag edges, correlations/regime relationships and strategy knowledge. No raw tick/event backlog.

## Reset

Development/initial production cutover may use the explicit reset procedure. Existing market-data history is intentionally not migrated. The reset must be repeatable and must validate that all legacy V2/V3 Redis groups and old market streams are absent before the new gateway starts.

## Failure model

Every failure belongs to one of these stages:

1. `transport` — connection, DNS, TLS, WS protocol.
2. `decode` — invalid payload/schema.
3. `sequence` — missing/out-of-order book update.
4. `queue` — producer/consumer imbalance.
5. `state` — state update/coherence failure.
6. `persistence` — ClickHouse/Neo4j write failure.
7. `feature` — calculation failure.
8. `scanner` — ranking/promotion failure.
9. `strategy` — decision failure.
10. `execution` — order lifecycle failure.

Alerts should identify the stage and symbol rather than reporting a generic "market data stale" error.
