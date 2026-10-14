# Data Flow Observability

This document implements the three-phase observability plan for AITOS market-data
lineage: **consolidate telemetry → pipeline dashboard signals → event-level debugger**.

OpenTelemetry is intentionally deferred. AITOS already has component telemetry,
`/health`, `/metrics`, forensics, and Grafana; the gap is **end-to-end correlation**.

## Pipeline stages

```
transport → receive → decode → normalize → gateway_queue →
redis_publish → redis_consumer → live_state → feature → scanner → trade_lifecycle
```

Canonical trace id: `md:{SYMBOL}:{event_id}` (same pattern as existing forensic helpers).

## Modules

| Module | Role |
|--------|------|
| `aitos/forensics/data_flow_schema.py` | Stages, issue classes, timestamps, trace_id |
| `aitos/forensics/data_flow_registry.py` | Counters, rates, p50/p95/p99 latency |
| `aitos/forensics/data_flow_debugger.py` | Symbol snapshot + pressure-point detection |
| `aitos/forensics/data_contract.py` | Stage checkpoints (stale vs corrupted) |

## Phase 1 — consistent schema (landed)

Record at each boundary (when instrumented):

- `trace_id`, `stage`, `symbol`, `event_id`
- `source_timestamp`, `received_at`, stage enter/exit times
- classified issues: `stale`, `duplicate`, `schema_invalid`, `semantic_invalid`,
  `sequence_gap`, `decode_error`, `dropped_backpressure`, `processing_timeout`

Registry API:

```python
from aitos.forensics.data_flow_registry import get_data_flow_registry
from aitos.forensics.data_flow_schema import PipelineStage

reg = get_data_flow_registry()
reg.record_input(PipelineStage.RECEIVE, symbol="BTCUSDT", source_age_ms=32)
reg.record_output(PipelineStage.RECEIVE, symbol="BTCUSDT", latency_ms=1.2)
```

## Phase 2 — dashboard signals

Expose via `/data-flow` (JSON) and Prometheus-friendly fields on stage snapshots:

- throughput (`input_rate` / `output_rate`)
- freshness (`source_age_ms`, `oldest_event_age_ms`)
- latency percentiles (p50 / p95 / p99 / max)
- queue depth, drops, errors
- pressure flag when `input_rate > output_rate` and age/queue grow

Grafana starter dashboard: `deploy/grafana/aitos_data_flow.json`.

## Phase 3 — event-level Data Flow Debugger

```
GET /data-flow
GET /data-flow/{symbol}
```

Example pressure diagnosis:

```
Redis publish in=12470 out=9100 lag=3370  ← pressure_point
```

## Diagnostic rule (unchanged from OBSERVABILITY_V1)

Inspect Exchange → Decoder → Queue → EventBus → State → Persistence → Feature → Scanner.
The first stage where input exceeds output and oldest age rises is the pressure point.

## Safety

These modules are observational only. They must not drop, reorder, or alter
trading decisions. Production behavior changes require a separate reviewed PR.
