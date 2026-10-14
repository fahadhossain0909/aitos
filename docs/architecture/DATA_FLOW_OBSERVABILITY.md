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
| `aitos/forensics/data_flow_hooks.py` | Safe no-raise wrappers for hot-path calls |

## Instrumentation map (landed)

| Stage | Wired in |
|-------|----------|
| `RECEIVE` | `StreamTelemetry.mark_received` |
| `DECODE` | `StreamTelemetry.mark_parsed` |
| `GATEWAY_QUEUE` | `MarketDataGateway.accept` / `_accept_latest` |
| `REDIS_PUBLISH` | `MarketDataGateway.drain_once` + `MarketDataBus.publish` |
| `REDIS_CONSUMER` | `MarketDataBus.subscribe` handler wrapper |
| `LIVE_STATE` | `CanonicalScannerFeed`, `LiveScannerCache.accept_*`, `MarketStateEngine.compute` |
| `FEATURE` | `MarketStateEngine.compute` + `OpportunityScanner.scan_symbol` |
| `SCANNER` | `OpportunityScanner.scan_symbol` |
| `TRADE_LIFECYCLE` | `TradeLifecycle.submit_opportunity` / `update_price` / `_trigger_exit` |

## Phase 2 — dashboard signals

- `GET /data-flow` and `GET /data-flow/{symbol}`
- `/metrics` exports `aitos_data_flow_*` gauges per stage
- Grafana starter: `deploy/grafana/aitos_data_flow.json`

## Safety

Observational only. Hot-path hooks go through `data_flow_hooks` and swallow exceptions.
