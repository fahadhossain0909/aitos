# AITOS Market Data Architecture v1

## Goals

- One canonical market-data pipeline for Binance Spot, USD-M Futures, COIN-M Futures and Options market data.
- No V2/V3 consumer-group generations. Names describe purpose, not deployment history.
- REST recovery is explicitly marked `rest` and can never silently become `websocket` live state.
- Hot state is bounded. Historical data lives in ClickHouse. Neo4j stores relationships, not stream traffic.
- BTC is the continuous market-structure anchor. BTC and LTC retain 100-level historical order books (50 bid + 50 ask).
- The rest of the Binance universe is scanned cheaply first; expensive order-flow/AMT/footprint analysis is activated only for promoted candidates.
- Every stage exposes the same telemetry vocabulary so a stuck pipeline is diagnosable without reading application logs blindly.

## Pipeline

```text
Binance Spot / USD-M / COIN-M / Options
                |
                v
        Market Data Gateway
   WS + REST recovery + sequence validation
                |
                v
       Canonical Market Events
                |
        +-------+--------+
        |       |        |
        v       v        v
      Redis  ClickHouse  Neo4j
      hot      history   relationships
        |       |
        +---+---+
            v
      Feature Engine
            |
      Universe Scanner
   ALL -> Top25 -> Top10 -> Top5 -> Top2
            |
      Expensive Analysis
  BTC continuous + promoted assets
            |
      Strategy / AI / Risk
            |
      Execution Router
      Spot / Futures
```

## Data ownership

### Redis

Redis is **hot state and short-lived transport**, never the historical source of truth. Every stream must have a bounded retention policy. Consumer groups are created once and named semantically. A service restart must not create a new numbered consumer generation.

### ClickHouse

ClickHouse is the historical source of truth. Store normalized raw market events and durable derived features. BTC/LTC order-book history is stored at 100 levels. Other symbols store market-wide metrics and only temporary/deep-book samples when promoted by the scanner.

### Neo4j

Neo4j stores instrument relationships, market relationships and learned lead/lag/correlation edges. It is not on the hot path for every trade or order-book delta.

## Live-state invariants

A market state is `websocket_live_state` only when all required components are fresh and coherent:

- transport connected;
- trade freshness within configured threshold;
- order-book freshness within configured threshold;
- sequence is valid;
- event timestamps are monotonic enough for the market;
- no active resync is pending.

If any invariant fails, the state is `degraded_rest` (or `unavailable`), never live. REST recovery may repair a local book or provide a temporary analytical snapshot, but it must retain its source label and age.

## Backpressure

There is one bounded queue per high-rate transport boundary. Do not create an additional unbounded queue between Redis, the scanner and the feature engine. Queue depth, wait count, drops/rejections, consumer lag and processing latency are emitted as telemetry. When a queue approaches capacity, the service reports degradation before it reaches a silent stall.

## Telemetry contract

Every transport/consumer reports:

- received, parsed, published, processed;
- rejected, stale, errors;
- reconnects and sequence gaps;
- queue waits and maximum queue depth;
- last event time, last ingest time and computed source age;
- consumer lag/pending count where a Redis consumer is involved.

High-rate paths use counters and periodic summaries, not one INFO log per message.

## Scanner tiers

1. **Universe:** cheap ticker/volume/volatility/funding/open-interest/basis/liquidation/regime features.
2. **Top 25:** add lightweight flow and liquidity features.
3. **Top 10:** add stronger order-flow and cross-market features.
4. **Top 5:** enable deeper book and footprint features.
5. **Top 2:** full expensive analysis.
6. **BTC:** remains continuously promoted as the structural anchor.

Promotion is dynamic; symbols are not hard-coded to BTC/ETH/SOL/BNB. Subscription depth follows the current tier.

## Cross-market features

The relationship engine can evaluate BTC -> alt lead/lag, Spot -> Futures and Futures -> Spot relationships, basis/funding/open-interest changes, liquidation cascades, BTC dominance/regime, and options IV/skew/term structure/OI/expiry concentration. These are features, not hard-coded trade rules.

## Fresh reset

Because the project is still under active development and historical data is explicitly disposable, the migration target is a reproducible empty state. A reset must be explicit, logged and idempotent; it must never be hidden inside application startup.
