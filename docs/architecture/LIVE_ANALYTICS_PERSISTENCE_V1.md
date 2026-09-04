# Live Analytics Persistence V1

## Purpose

ClickHouse is the durable analytical memory for AITOS. Live market-data processing,
trading decisions, risk controls and execution remain independent of ClickHouse
latency or availability.

## Persisted live layers

The `live_analytics_events` table stores auditable events from:

- `decision.*` — scanner/kernel/contextual decision records, confidence, rationale and evidence references.
- `risk.*` — risk scores, gate decisions, exposure/protection events and emergency controls.
- `trade.*` — position lifecycle, fills/outcomes and trade journey events emitted by the trading path.
- `execution.*` — execution/order/fill telemetry when emitted by an execution adapter.
- `journey.*` — Trade Journey state, health, path adherence and management actions.
- `intelligence.*` — contextual/AI-derived intelligence events.
- `statistics.*` — statistical model outputs and probability/regime telemetry when emitted on this namespace.
- `scanner.*` — opportunity/ranking telemetry when emitted on this namespace.

Market OS has dedicated typed tables for high-volume order-flow, liquidity and
live-state data. The generic analytics table is intentionally schema-flexible so
new AI/statistical models can add fields without a ClickHouse migration for every
new model version.

## Freshness invariant

All live analytics subscriptions use `live_only=True`. A restart therefore starts
from the current Redis stream position instead of replaying an old backlog into
the live analytical path.

## Backpressure invariant

Analytics persistence is batched. A slow ClickHouse does not become a dependency
of the exchange websocket, scanner, decision, risk or execution path. Queue/batch
pressure is observable and persistence errors are counted.

## Historical separation

Historical deep order-book collection remains a separate policy and is fixed to
`BTCUSDT` and `LTCUSDT`. It must never be derived from the live ranking policy.

## Event lineage

Each analytics row keeps:

- event time and ingest time
- event ID
- category
- exchange and market
- symbol
- source module
- correlation ID
- schema version
- complete JSON payload

This permits decision-to-outcome reconstruction and later model attribution,
replay, calibration and learning without changing the live trading contract.
