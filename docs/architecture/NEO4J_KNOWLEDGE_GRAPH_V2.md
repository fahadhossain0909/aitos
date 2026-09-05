# Neo4j Knowledge Graph V2

Neo4j is AITOS's semantic relationship and knowledge layer; Redis/EventBus remains live transport, while ClickHouse remains durable numerical/training history.

## V2.3 integration

Neo4j historical cases are now exposed to the contextual decision path as optional evidence. The scanner bridge retrieves compact cases by symbol/regime/strategy/model, converts resolved outcomes into the standard 0..10 evidence scale, and adds `graph_historical_support` to the governed fusion inputs.

The graph prior is deliberately bounded and non-authoritative:

```text
Current market evidence
        +
Contextual / statistical evidence
        +
Neo4j historical semantic cases
        ↓
Decision Fusion
        ↓
Policy / Risk Governance
        ↓
Execution Intent
```

A Neo4j outage, missing credentials, empty graph, or retrieval failure degrades this component to unavailable; it never blocks scanner discovery or creates a direct trade instruction. The existing ClickHouse/Neo4j storage boundary is preserved.

## Graph retrieval

`aitos.knowledge_graph.retrieval.GraphContextRetriever` provides read-only `similar_cases(...)` retrieval. Matching priority is symbol, regime, strategy, then model. Only compact semantic event metadata and resolved outcome/PnL are returned.

`aitos.intelligence.graph_context.retrieve_graph_context(...)` adapts that retrieval into a bounded contextual feature. `graph_historical_support` is included in the same evidence-fusion framework as the existing market evidence and is never treated as a standalone signal.

## Controlled reconstruction

`aitos.knowledge_graph.backfill.ClickHouseNeo4jBackfill` remains a maintenance-only ClickHouse -> Neo4j reconstruction path. It reads semantic `live_analytics_events`, uses bounded batches/keyset pagination, and reuses idempotent graph projection. Raw ticks/L2 are never copied to Neo4j.

## Schema

`aitos.knowledge_graph.schema` defines idempotent uniqueness constraints for stable semantic IDs and indexes for symbol, regime, topic, and event time. Apply these during Neo4j provisioning/maintenance rather than inside the high-frequency decision loop.

## Governance rules

1. Graph context is evidence/context, never a direct trade command.
2. Current market evidence remains primary; graph history cannot bypass policy or risk.
3. Missing graph data is unavailable, not neutral market evidence.
4. Neo4j failures must not block live ingestion or execution.
5. Numerical training, calibration curves, raw ticks and L2 remain ClickHouse/artifact responsibilities.
6. Historical deep order-book collection remains fixed to `BTCUSDT` + `LTCUSDT`.

## Implementation status

### V2 — complete
- Event-driven semantic projection and live-only semantic subscriptions.
- Trade, strategy, mistake, correlation and semantic lineage graph.

### V2.1 — complete
- Evidence, Forecast, Outcome, RiskDecision, Execution, TradeJourney, Model, Policy, ModelRun and CalibrationRun lineage.

### V2.2 — complete
- Controlled ClickHouse -> Neo4j reconstruction.
- Bounded/keyset replay and idempotent projection.
- Neo4j constraints/index definitions.
- Read-only semantic case retrieval.

### V2.3 — implemented
- Graph historical context is injected into contextual scanner decisions.
- `graph_historical_support` is a governed fusion feature with bounded influence.
- Retrieval failure is fail-open for the optional feature and fail-closed for unsafe graph-derived actions.
- Regression coverage added for graph-context scoring.

### V3 — future
- Temporal/causal relationship analysis.
- Regime-transition graph analytics.
- Strategy-to-market compatibility maps.
- Model failure-mode discovery and explainable retrieval.
