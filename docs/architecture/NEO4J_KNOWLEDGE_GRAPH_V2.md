# Neo4j Knowledge Graph V2

## Purpose

Neo4j is AITOS's **semantic relationship and knowledge layer**. It is not a replacement for Redis or ClickHouse and must not become a high-frequency market-data sink.

The three stores have explicit responsibilities:

| Layer | Responsibility | Data shape |
|---|---|---|
| Redis/EventBus | live transport + hot operational state | high-frequency events, current state |
| ClickHouse | durable analytical/training history | ticks, order book, indicators, features, decisions, outcomes |
| Neo4j | relationships, lineage, semantic memory | trades, strategies, models, policies, regimes, decisions, evidence, outcomes, correlations |

## Data flow

```text
Exchange
  -> MarketData adapters
  -> Redis/EventBus
      -> live trading / scanner / AI / statistics / risk / execution
      -> ClickHouse persistence
      -> Neo4j KnowledgeGraphWriter (selected semantic events only)

ClickHouse
  -> backtest / statistics / calibration / ML-RL training
  -> graph backfill / reconstruction (controlled research operation)
```

### Why the graph writer consumes EventBus

Strategies and intelligence modules should **not write Neo4j directly**. They emit canonical events. This keeps trading logic independent of storage and prevents a Neo4j outage from becoming a trading outage. The current writer subscribes to decision, risk, scanner, statistics, intelligence, journey and execution namespaces with `live_only=True`.

Raw market streams (`market.trade.*`, order-book deltas, klines) are deliberately excluded from the semantic graph writer.

## Canonical graph model

Core nodes:

- `Symbol` / `Instrument`
- `Trade`
- `Strategy`
- `Decision`
- `RiskDecision`
- `Execution`
- `TradeJourney`
- `MarketRegime`
- `Evidence`
- `Signal` / `Feature`
- `Model`
- `Policy`
- `Forecast`
- `Outcome`
- `Mistake`
- `KnowledgeEvent`
- `CalibrationRun` / `ModelRun`

Core relationships:

```text
Decision -[:ABOUT_SYMBOL]-> Symbol
Decision -[:SUPPORTED_BY]-> Evidence
Decision -[:GENERATED_BY]-> Model
Decision -[:GOVERNED_BY]-> Policy
Decision -[:IN_REGIME]-> MarketRegime
Decision -[:RESULTED_IN]-> Trade
Trade -[:USED_STRATEGY]-> Strategy
Trade -[:HAS_JOURNEY]-> TradeJourney
Trade -[:HAD_RISK_DECISION]-> RiskDecision
Trade -[:HAS_EXECUTION]-> Execution
Trade -[:RESULTED_IN]-> Outcome
Trade -[:HAD_MISTAKE]-> Mistake
Model -[:PRODUCES]-> Forecast
Forecast -[:RESOLVED_AS]-> Outcome
Model -[:EVALUATED_IN]-> CalibrationRun
Symbol -[:CORRELATED_WITH {coefficient, updated_at}]-> Symbol
```

`KnowledgeEvent` provides immutable-ish event lineage and connects the event to canonical entities. It is intentionally bounded to semantic events rather than market ticks.

## Statistical models and probability calibration

Neo4j should **not perform the heavy numerical calibration itself**. Calibration is computed by Python/statistical services from ClickHouse data. Neo4j stores the relationships needed to explain and query the result:

```text
Model/ModelVersion
  -> Forecast {probability, horizon, target}
  -> Outcome {realized_label, pnl, ...}
  -> CalibrationRun {method, sample_count, brier, log_loss, ece, ...}
```

This supports questions such as:

- Which model is calibrated best in trending regimes?
- Does a 0.80 probability forecast actually resolve near 80% historically?
- Which strategy/model combination is over-confident on a particular symbol?
- Which calibration run produced the currently governed policy?

The numerical time series and raw samples remain in ClickHouse; Neo4j stores lineage and semantic links.

## AI / explainability

AI outputs should be represented as decision lineage, not just text:

```text
Decision
  -> Evidence -> Feature/Signal
  -> GeneratedBy -> Model
  -> GovernedBy -> Policy
  -> InRegime -> MarketRegime
  -> About -> Symbol
```

Feature attribution (SHAP/attention/etc.) can be stored as compact evidence records for important decisions. Large explanation arrays should remain in ClickHouse/artifacts, with a graph reference or artifact ID.

## Learning and deep learning

Training jobs remain ClickHouse-first. A completed training run should create/update a `Model`/`ModelVersion` and `ModelRun`/`CalibrationRun` node containing:

- dataset/window identifier
- feature-set version
- architecture/version
- hyperparameter-set ID
- validation/holdout metrics
- calibration metrics
- artifact/model URI
- parent model/champion relationship
- promotion/governance state

Learning curves should normally be stored as compact checkpoints/metrics in ClickHouse or model artifacts. Neo4j links the curve/run to model lineage rather than storing every batch point as graph nodes.

## Trade Journey integration

The graph is especially valuable for Trade Journey analysis. Journey state transitions can connect:

```text
Trade -> TradeJourney -> StateTransition
Trade -> Decision -> Evidence/Model/Policy
Trade -> RiskDecision
Trade -> Execution
Trade -> Outcome
```

This allows future analysis of patterns such as which evidence combinations commonly lead from `PROVING` to `CONFIRMED`, or which conditions precede `DECAYING`/`EXITING`.

## Source-of-truth rules

1. Redis is the live operational source, not the historical authority.
2. ClickHouse is the durable analytical source of truth.
3. Neo4j is the relationship/semantic source of truth for graph questions.
4. Neo4j writes are asynchronous and must never block market-data ingestion or order execution.
5. Strategies/intelligence do not call Neo4j directly; they emit events.
6. High-frequency raw market data is never mirrored wholesale into Neo4j.
7. Research/backfill from ClickHouse is controlled and idempotent.
8. Historical deep order-book collection remains the fixed `BTCUSDT` + `LTCUSDT` research policy and is independent of the live Top-2 order-book policy.

## Phased implementation

### V2 now

- Event-driven semantic projection for decision/risk/scanner/statistics/intelligence/journey/execution.
- Trade, strategy, mistake and symbol-correlation graph.
- Live-only subscriptions for semantic events.
- No raw tick/L2 ingestion into Neo4j.

### V2.1 next

- First-class `Evidence`, `Forecast`, `Outcome`, `RiskDecision`, `Execution` and `TradeJourney` nodes with stable IDs.
- Model/version and policy lineage.
- Calibration-run nodes linked to forecasts/outcomes.

### V2.2 research

- Controlled ClickHouse -> Neo4j backfill/reconstruction.
- Graph-based similarity/retrieval for regime/strategy/trade cases.
- Graph features exposed to the AI contextual decision layer.
- Graph-derived priors used as **features**, never as an ungoverned direct trading decision.

### V3

- Temporal/causal relationship analysis.
- Regime-transition graph analytics.
- Strategy-to-market compatibility maps.
- Model failure-mode discovery and explainable retrieval for autonomous decision support.
