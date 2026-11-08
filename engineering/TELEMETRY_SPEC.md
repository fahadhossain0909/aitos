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

## Existing source-verified telemetry surfaces (2026-11-03)
- Scanner: `OpportunityScanner.scan_symbol()` records `PipelineStage.SCANNER` input/output latency with a monotonic clock, trace start/completion, candidate-found flag and processing errors.
- Decision funnel: `run_scan_and_trade_cycle()` logs candidate component scores/weights/threshold, kernel direction/confidence rejections, position-cap skips, decision snapshots, lifecycle/risk rejection and accepted/submitted state.
- Position lifecycle: `position_runtime.py` emits evaluation duration, monitor tier/priority, health snapshot, action/reason, monitor heartbeat/staleness and close attempt/completed/failed events. These are structured log events, not all Prometheus histograms.
- `position_lifecycle_telemetry_report.py` reconstructs per-position evaluation freshness and close evidence from JSON-lines logs.
- The source contains meaningful event coverage, but event completeness, actual log delivery/retention, cross-stage trace correlation and real latency distributions remain NOT RUNTIME-VERIFIED.

## Outstanding telemetry verification gaps
- No runtime evidence yet confirms the complete exchange -> Redis -> consumer -> scanner -> decision -> execution -> fill -> persisted position -> close -> reconciliation chain.
- Verify whether order submit/ack/fill and close-confirmation durations can be reconstructed from current lifecycle events and logs before adding new metrics; avoid duplicate telemetry.
- Prometheus currently exposes data-flow stage percentiles and module health, while detailed per-position reasons/identities belong in structured logs. Validate dashboard/alert consumers and cardinality in the actual deployment.
- The in-memory data-flow bucket dictionary has no TTL/eviction in the inspected source (AITOS-ENG-009); measure symbol churn and RSS before proposing a cap.

## Initial source-audit finding and remediation (2026-11-03)
- `aitos/forensics/data_flow_registry.py` represents an empty latency sample set as `None` in `StageSnapshot`.
- `aitos/health_server.py::_append_data_flow_metrics()` currently exports missing p50/p95/p99 values as numeric zero.
- This collapses “no observations” and “observed zero latency” into the same Prometheus value. Treat as a telemetry correctness defect (AITOS-ENG-004). Runtime/dashboard impact is NOT TESTED.
- The current contract test checks metric-name presence but does not assert missing-sample semantics.
- Implemented on `feat/engineering-agent-operating-system`: latency percentile series are omitted until observed; `aitos_data_flow_latency_sample_count` explicitly reports availability (including zero); a real observed 0.0 ms remains a valid percentile. Grafana and regression tests were updated. CI and runtime verification remain pending.

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

## Addendum — local projection observation, not trading readiness (2026-11-06)

Optional snapshot `projection` schema 1 exposes fixed statuses, UTC observation
time, local outbox total/pending/delivered and last checked/inserted/acknowledged
batch. Complete ACK, safe integers and conservation required. Expiry >60s or
future skew >5s makes evidence unavailable; failures clear counts. No account/event
IDs, raw payloads, secrets, exceptions or arbitrary extras. Provider absent is
unavailable, never a synthetic healthy value. Both authorization/certification
flags remain false. No new Prometheus metrics or high-cardinality labels.

Backend/TypeScript contract, real isolated API-to-built-browser flow, malformed
clearing and mobile layout VERIFIED. Continuous monitoring, historical sink
integrity, remote-service soak and production SLO are not established. See
`PROJECTOR_AND_PERSISTENCE_CONSOLE_2026-11-06.md`.

## Shadow worker and reconciliation evidence (2026-11-06)

Optional paper worker publishes the existing sanitized expiring projection schema;
record failure is latched unavailable, never healthy counters over a known gap.
No new Prometheus family, sensitive account labels or raw error payloads. Offline
account comparison outputs only status and symmetric-difference record counts;
match is not authorization. Service workflow reports are synthetic test evidence
only. Windows/signing/remote CI and independent review remain NOT TESTED. See
`WINDOWS_PAPER_INTEGRATION_2026-11-06.md`.

## Finishing tranche — 2026-11-06

Service reports now record synthetic provenance, stage, UTC timestamp, validated source SHA and observed server version. Connected recovery checks the seeded version; offline evidence does not invent a current version. Native installed success evidence is emitted only after uninstall passes. See `PAPER_NATIVE_HARDENING_2026-11-06.md`.

## Recorded account boundary — 2026-11-06

Provider adapter emits sanitized comparison status/counts only. Unknown/duplicate/truncated/stale/context-mismatched evidence is unavailable. No account IDs, amounts, raw provider extras, errors or metric labels exported; flags never authorize trading. See `BINANCE_ACCOUNT_EVIDENCE_2026-11-06.md`.

## Hosted validation handoff

Actual remote job/step results and artifact digests now retained via GitHub links. JUnit, synthetic service journals/stage reports, unsigned Windows candidate/install evidence are available. Raw payload/log download redirects are blocked in this sandbox; no claim of independent binary inspection. See `REMOTE_VALIDATION_EVIDENCE.md`.

## Sustained-write harness correction

Corrected service reports add workload=continuous_unique_events; iterations must equal newly inserted soak rows. Historical reports remain identifiable by source SHA and lack this label. No fabricated throughput/percentiles. See `REMOTE_VALIDATION_EVIDENCE.md`.
