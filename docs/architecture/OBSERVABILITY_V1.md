# Market Data Observability V1

Every market-data component must expose the same small set of signals:

| Signal | Meaning |
|---|---|
| input_rate | events entering the stage/sec |
| output_rate | events leaving the stage/sec |
| queue_depth | current bounded backlog |
| oldest_event_age_ms | age of oldest queued event |
| source_age_ms | age of exchange event at observation |
| processing_latency_ms | stage processing time |
| reconnect_count | transport reconnects |
| sequence_gap_count | order-book continuity failures |
| dropped_count | explicit loss/coalescing |
| error_count | failures by stage |
| degraded | stage is operating without full live guarantees |

## Diagnostic rule

When data appears stuck, inspect in order:

1. Exchange transport: connected + recent receive timestamp.
2. Decoder: receive count vs parse count.
3. Gateway queue: depth and oldest age.
4. Event bus: publish count vs consumer count and lag.
5. State: last trade/book timestamps per symbol.
6. Persistence: ClickHouse insert rate/errors.
7. Feature engine: input/output rate and latency.
8. Scanner: candidate refresh timestamp and promotion state.

The first stage where `input_rate > output_rate` and `oldest_event_age_ms` increases is the pressure point. Do not infer the cause from a downstream symptom.

## Logging policy

- INFO: lifecycle transitions, reconnects, resyncs, degraded-mode entry/exit, periodic health summary.
- WARNING: stale source, growing queue, sequence gap, dropped/coalesced data.
- ERROR: decode failure, persistence failure, unrecoverable state failure.
- DEBUG: individual payload/event diagnostics; never log every high-rate event at INFO.

Every warning/error includes `component`, `stage`, `venue`, `market_type`, `symbol`, `event_id` where available, and relevant age/queue/sequence values.

## Alert thresholds

Thresholds are configuration, not hard-coded business logic. Initial defaults:

- critical live source age: > 15s
- warning queue utilization: > 70%
- critical queue utilization: > 90%
- warning processing latency: > 1s
- critical processing latency: > 5s
- sequence gap: immediate warning and book resync
- repeated reconnects: warning when > 3 in 5 minutes

These thresholds are deliberately conservative for development and should be tuned from observed production distributions.
