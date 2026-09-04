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
  -> controlled graph backfill / reconstruction

Neo4j
  -> read-only graph context retrieval
  -> AI/contextual decision features
```

### Storage boundary

Strategies and intelligence modules **do not write Neo4j directly**. They emit canonical events. The graph writer consumes selected semantic events from EventBus with `live_only=True`. This keeps storage concerns out of trading logic and ensures a Neo4j outage does not become a trading outage.

Raw `market.trade.*`, order-book deltas, klines and large feature streams stay in Redis/ClickHouse. Neo4j stores compact semantic references and lineage.

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
KnowledgeEvent -[:ABOUT_SYMBOL]-> Symbol
KnowledgeEvent -[:INVOLVES_STRATEGY]-> Strategy
KnowledgeEvent -[:PRODUCED_BY_MODEL]-> Model
KnowledgeEvent -[:GOVERNED_BY_POLICY]-> Policy
KnowledgeEvent -[:RELATES_TO_DECISION]-> Decision
KnowledgeEvent -[:RELATES_TO_TRADE]-> Trade
KnowledgeEvent -[:OCCURRED_IN_REGIME]-> MarketRegime
KnowledgeEvent -[:SUPPORTED_BY]-> Evidence
KnowledgeEvent -[:HAS_RISK_DECISION]-> RiskDecision
KnowledgeEvent -[:HAS_EXECUTION]-> Execution
KnowledgeEvent -[:HAS_JOURNEY]-> TradeJourney
KnowledgeEvent -[:REFERENCES_FORECAST]-> Forecast
KnowledgeEvent -[:REFERENCES_OUTCOME]-> Outcome
KnowledgeEvent -[:PART_OF_MODEL_RUN]-> ModelRun
KnowledgeEvent -[:PART_OF_CALIBRATION]-> CalibrationRun

Trade -[:ON_SYMBOL]-> Symbol
Trade -[:USED_STRATEGY]-> Strategy
Trade -[:HAD_MISTAKE]-> Mistake
Symbol -[:CORRELATED_WITH {coefficient, updated_at}]-> Symbol
```

All first-class semantic nodes use stable IDs supplied by the event when available. Evidence without a supplied ID receives an event-scoped deterministic ID. Trade and mistake projections use `MERGE`, making repeated event delivery idempotent at the node level.

## Statistical models and probability calibration

Neo4j does **not** perform heavy numerical calibration. Python/statistical services compute calibration from ClickHouse data and publish the resulting semantic run.

```text
Model
  -> Forecast {probability, horizon, target}
  -> Outcome {realized_label, pnl, ...}
  -> CalibrationRun {method, sample_count, brier, log_loss, ece, ...}
```

This supports graph questions such as:

- Which model is calibrated best in trending regimes?
- Does a 0.80 probability forecast resolve near 80% historically?
- Which strategy/model combination is over-confident on a symbol?
- Which calibration run produced the governed policy?

Raw samples, calibration curves and numerical time series remain in ClickHouse or model artifacts. Neo4j retains lineage and semantic relationships.

## AI / explainability

AI outputs are represented as decision lineage:

```text
Decision
  -> Evidence -> Feature/Signal
  -> Model
  -> Policy
  -> MarketRegime
  -> Symbol
  -> Trade
```

Compact SHAP/attention feature attribution may be projected as `Evidence`. Large explanation tensors/arrays remain in ClickHouse or artifacts and are referenced by ID/URI.

## Learning and deep learning

Training remains ClickHouse-first. A completed training event can project:

```text
Model
  -> ModelRun
      -> dataset_id
      -> feature_set_version
      -> artifact_id
      -> validation / holdout metrics
      -> promotion status
  -> CalibrationRun
```

Learning curves remain numerical artifacts/ClickHouse records rather than thousands of graph nodes. Neo4j provides the lineage needed to connect a deployed model to its dataset, run, calibration, policy and downstream outcomes.

## Trade Journey integration

Journey events can attach a stable `TradeJourney` to a trade and decision lineage:

```text
Trade -> TradeJourney
Trade -> Decision -> Evidence/Model/Policy
Trade -> RiskDecision
Trade -> Execution
Trade -> Outcome
```

This enables later retrieval of patterns such as which evidence combinations commonly precede `PROVING -> CONFIRMED`, or which model/regime combinations precede `DECAYING -> EXITING`.

## V2.2 controlled backfill

`aitos.knowledge_graph.backfill.ClickHouseNeo4jBackfill` is a maintenance/reconstruction component, not a live runtime worker.

Properties:

- Reads only `live_analytics_events` semantic namespaces (`decision.*`, `risk.*`, `scanner.*`, `statistics.*`, `intelligence.*`, `journey.*`, `execution.*`).
- Uses bounded batches (default 500, maximum 5000).
- Uses ClickHouse keyset pagination by `(event_time, event_id)` so large histories do not require `OFFSET` scans.
- Uses the same `MERGE`-based semantic projection as live events, so replaying a window is idempotent.
- Accepts `start`, `end`, and optional `max_batches`, allowing staged recovery/backfill.
- Never reads or mirrors raw ticks/order-book rows into Neo4j.

The job should be invoked by a maintenance/backfill process with its own resource limits. It must not be inserted into the latency-sensitive ingestion loop.

## Graph schema hardening

`aitos.knowledge_graph.schema` provides idempotent uniqueness constraints for stable entity IDs and indexes for symbol, regime, topic and event time. These are safe to apply during Neo4j maintenance/startup provisioning and are intentionally separate from high-frequency writes.

## Graph retrieval for AI

`aitos.knowledge_graph.retrieval.GraphContextRetriever` provides a read-only `similar_cases(...)` query. It ranks historical semantic cases by matching:

1. symbol — strongest match,
2. market regime,
3. strategy,
4. model.

The result is deliberately compact: event/topic/time plus symbol, regime, strategy/model and realized outcome/PnL. The retrieved graph context is **evidence**, not an instruction. The AI contextual decision layer must combine it with current market state, statistical outputs and risk/policy governance before producing a decision.

## Operational rules

1. Redis/EventBus = live transport and hot state.
2. ClickHouse = durable numerical/analytical source of truth.
3. Neo4j = semantic relationship/lineage source of truth for graph questions.
4. Neo4j writes are isolated from the live trading path.
5. Strategies/intelligence never call Neo4j directly.
6. Raw high-frequency market data is never mirrored wholesale into Neo4j.
7. Controlled ClickHouse -> Neo4j reconstruction must be idempotent.
8. Graph-derived information is a **context feature**, never an ungoverned direct trade instruction.
9. Historical deep order-book collection remains fixed to `BTCUSDT` + `LTCUSDT`, independent of live Top-2 order-book ranking.

## Implementation status

### V2 — complete

- Event-driven semantic projection for decision/risk/scanner/statistics/intelligence/journey/execution.
- Trade, strategy, mistake and symbol-correlation graph.
- Live-only semantic subscriptions.
- No raw tick/L2 ingestion into Neo4j.

### V2.1 — implemented

- First-class `Evidence`, `Forecast`, `Outcome`, `RiskDecision`, `Execution` and `TradeJourney` nodes.
- Stable model/policy and training-run lineage.
- Calibration-run metadata and forecast/outcome references.
- Bounded evidence projection for features/attributions.
- Idempotent trade/mistake node projection.
- Failure isolation and health counters retained.

### V2.2 — implemented

- Controlled ClickHouse -> Neo4j backfill/reconstruction worker.
- Keyset pagination and bounded maintenance windows.
- Idempotent graph reconstruction using the same semantic projection query.
- Neo4j uniqueness constraints/index definitions.
- Read-only graph similarity/context retrieval for regime/strategy/trade cases.

### V2.3 — integration target

- Wire `GraphContextRetriever` into the AI contextual decision layer as a governed context/evidence provider.
- Add end-to-end integration tests covering ClickHouse event -> backfill -> Neo4j -> contextual retrieval.
- Add scheduled maintenance entrypoint for controlled backfill, with explicit resource limits and observability.

### V3 — future

- Temporal/causal relationship analysis.
- Regime-transition graph analytics.
- Strategy-to-market compatibility maps.
- Model failure-mode discovery and explainable retrieval for autonomous decision support.
