# AITOS Telemetry Specification

## Goals
Telemetry must describe actual system behavior from exchange input through trading outcome. A reachable process or populated metrics endpoint alone is insufficient.

## Signal types
- Metrics: bounded-cardinality counters, gauges, and histograms.
- Logs: structured event details and high-cardinality identifiers.
- Traces: causal path across stages using trace/correlation IDs.
- Health/readiness: dependency and module readiness, not just process liveness.

## Required dimensions
Use bounded labels such as environment, mode (paper/testnet/live), component, stage, event type, outcome, and venue where appropriate. Never use event IDs, order IDs, position IDs, arbitrary symbols at unbounded scale, exception text, or user/account identifiers as Prometheus labels. Put those identifiers in structured logs/traces.

## Metric families to implement or verify
| Domain | Suggested metric family | Type | Notes |
|---|---|---|---|
| Process | process CPU, RSS, uptime, event-loop lag | gauge/histogram | Include host/container identity |
| Module | module readiness, startup duration, restart/failure count | gauge/histogram/counter | Distinguish initialized from operational |
| Exchange | connection state, reconnects, received events, stale/invalid events | gauge/counter | Split REST and WebSocket sources |
| Data pipeline | stage processing duration, events in/out, dropped events | histogram/counter | Label stage and bounded event type |
| Event bus | publish/consume/ack/fail/retry/DLQ, pending count, oldest age | counter/gauge/histogram | Per bounded stream/consumer class |
| Storage | operation duration, insert failures, pool saturation, rows written | histogram/counter/gauge | No SQL text labels |
| Scanner | symbols observed, candidates generated/ranked/expired, evaluation duration | counter/gauge/histogram | Include reason codes with bounded enum |
| Decisions | entry accepted/rejected, HOLD/exit decisions, reason counts | counter | Keep strategy and reason taxonomy bounded |
| Execution | order intents, requests, acknowledgements, fills, rejects, retries | counter/histogram | Never log secrets |
| Position lifecycle | open/closed/partial counts, recovery, reconciliation mismatch | gauge/counter | Identity belongs in trace/log |
| Outcomes | realized/unrealized PnL, fees, slippage, MAE/MFE | histogram/gauge | Define units, sampling and source |
| Learning | experiences recorded, training jobs, failures, model version | counter/gauge | Avoid model/user high cardinality |

Metric names above are proposed families, not claims that these metrics already exist. Reuse current metric names/contracts where present.

## Latency boundaries
Capture monotonic durations within a process and UTC wall-clock timestamps for cross-process correlation. Cross-host one-way latency requires clock synchronization and explicit uncertainty; otherwise report receive-to-receive or queue residence durations rather than claiming precise network latency.

Required spans:
1. exchange receive -> decode start/end
2. decode -> validation end
3. validation -> publish
4. publish -> consumer receive (queue residence)
5. consumer receive -> handler end
6. feature calculation
7. scanner evaluation
8. strategy decision
9. risk validation
10. order submit -> acknowledgement
11. acknowledgement -> fill/terminal state
12. fill -> persistence
13. position monitoring interval and decision
14. exit decision -> close request
15. close request -> confirmation
16. end-to-end decision-to-outcome (with clear definition)

Report p50/p95/p99/max, sample count, window, mode, workload and missing data. Histograms must use suitable buckets and bounded labels.

## Trading attribution
Every decision should be reconstructable from logs/traces using stable decision, event, order, and position IDs (not metric labels). Record feature snapshot/version, candidate rank, decision action, accepted/rejected/HOLD/exit reason, risk verdict, execution result, and eventual outcome where available.

## Telemetry validation checklist
- Endpoint reachable and expected content type.
- Prometheus scrape succeeds.
- Expected metric names and labels present.
- Counters increase under controlled activity.
- Histograms have samples and correct units.
- Grafana panels query the actual metric names.
- Alert rules evaluate correctly where configured.
- Logs/traces correlate across service boundaries.
- No unbounded labels or secrets.
- Instrumentation overhead is measured.
- Missing instrumentation is marked NOT MEASURED, not zero.
