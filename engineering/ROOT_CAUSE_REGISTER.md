# AITOS Root-Cause Register

## Register rules
- One row per distinct incident or reproducible defect.
- Do not promote a hypothesis to root cause without causal evidence.
- Link evidence, logs, traces, tests, commits and PRs where available.
- Keep resolved incidents with their verification evidence; do not erase history.

## Initial register
| ID | Severity | Symptom | Root cause | Confidence | Status | Evidence / next action |
|---|---|---|---|---|---|---|
| AITOS-ENG-001 | — | Engineering operating model and durable docs were not present at repository root | Documentation gap; no runtime defect implied | VERIFIED (repository inspection) | IN PROGRESS | This branch adds repository-level contract and engineering docs |
| AITOS-ENG-002 | — | Runtime/latency baseline not yet captured by this work | Environment/runtime not accessed in this documentation change | VERIFIED (scope limitation) | NOT MEASURED | Run isolated paper-runtime audit and populate baseline |
| AITOS-ENG-003 | — | Open testnet integration PRs may overlap | Three open PRs #210, #213, #215 have overlapping Binance Futures Testnet scope | VERIFIED (GitHub PR search) | OPEN / REVIEW SEPARATELY | Do not modify or merge them as part of this documentation change |
| AITOS-ENG-004 | P2 | Data-flow latency exporter may publish 0 for stages with no latency observations | `_append_data_flow_metrics()` converts missing p50/p95/p99 values to numeric zero | FIX IMPLEMENTED — NOT VERIFIED | OPEN | `aitos/health_server.py`; `DataFlowRegistry.snapshot_stage()` returns None when sample deque is empty. Implemented separately in PR #217 (`fix/telemetry-missing-sample`): omit unobserved percentile series, export `aitos_data_flow_latency_sample_count`, update Grafana, and test both no-sample and genuine 0.0 ms cases. Await PR #217 CI and runtime verification. |
| AITOS-ENG-005 | P2 | Startup failure during paper-runner initialization may bypass graceful cleanup | The main `try/finally` begins after Redis, ClickHouse, exchange/universe, system modules, recovery, and auxiliary agents have already been initialized | VERIFIED (control-flow inspection); impact depends on failure point | OPEN | `run_paper_trading.py::main`; `shutdown_all()` is only reached in the later finally block. Recommend staged resource ownership/AsyncExitStack and injected-failure tests. Do not refactor until targeted startup/shutdown tests are available. |
| AITOS-ENG-006 | P2 | Self-heal coverage differs from health-report coverage | HealthServer receives core modules plus recorder, MarketOS persistence and four agents; periodic self-heal loop iterates only `components.all_modules()` | VERIFIED (source) | OPEN / DESIGN REVIEW | Decide whether auxiliary modules are restartable and include them only with explicit lifecycle ownership; otherwise label them monitored-only. |
| AITOS-ENG-007 | P2 | HTTP-only health probes may miss degraded module state | `/health` intentionally returns HTTP 200 when any module is DEGRADED; payload reports `status=degraded` | VERIFIED (source; intentional behavior) | DOCUMENTED | Consumers must inspect JSON status/module entries, not HTTP status alone. Keep behavior unless CD/readiness contract is explicitly changed. |
| AITOS-ENG-008 | P2 | Position-lifecycle API is not durable history | `_RECENT_EVENTS` is a process-local deque capped at 2,000; restart clears it and old events roll off | VERIFIED (source; documented limitation) | DOCUMENTED | Use durable structured logs/journal for historical forensics; do not treat absent event as proof no HOLD/exit occurred. |
| AITOS-ENG-009 | P2 | Data-flow registry retains per-symbol buckets for the process lifetime | `_buckets` is keyed by `(stage, symbol)` and has no eviction/TTL; memory impact remains unmeasured | VERIFIED (source); memory impact is still a HYPOTHESIS pending runtime churn/RSS measurement | INSTRUMENTED — RUNTIME BASELINE PENDING | Added `cardinality_snapshot()` plus Prometheus gauges for total buckets, symbol buckets, aggregate buckets, unique symbols and per-stage bucket counts. No eviction is introduced before runtime evidence; the later combined runtime pass must capture bucket count and process RSS during dynamic-universe rotation and then decide whether bounded LRU/TTL is justified. |
 
## Incident template
### AITOS-YYYY-NNN — short title
- Severity:
- First observed (UTC):
- Last observed (UTC):
- Mode/environment:
- Affected components:
- Status: INVESTIGATING / MITIGATED / FIX IMPLEMENTED — NOT VERIFIED / RESOLVED / BLOCKED
- Symptom:
- Evidence:
- Reproduction:
- First divergence:
- Confirmed root cause:
- Competing hypotheses:
- Downstream effects:
- Remediation:
- Approval required:
- Tests and results:
- Runtime verification:
- Rollback:
- Remaining risks:
- Next action:
