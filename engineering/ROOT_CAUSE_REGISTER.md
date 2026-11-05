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
| AITOS-ENG-006 | P2 | Self-heal coverage differed from health-report coverage | HealthServer received core modules plus recorder, MarketOS persistence and four agents; periodic self-heal loop iterated only `components.all_modules()` | VERIFIED (source) | FIX IMPLEMENTED — NOT VERIFIED | Added `restart_unhealthy_modules()` for explicitly owned auxiliary modules; paper self-heal now includes MarketOS persistence, LearningExperienceRecorder and the four AI agents. Their shutdown paths now reset initialized state; recorder and MarketOS subscriptions are registered incrementally and rolled back on partial failure; MarketOS health now reports a dead flush task as unhealthy. Added restart-order and module reinitialization tests. CI pending; runtime health recovery remains deferred. |
| AITOS-ENG-007 | P2 | HTTP-only health probes may miss degraded module state | `/health` intentionally returns HTTP 200 when any module is DEGRADED; payload reports `status=degraded` | VERIFIED (source; intentional behavior) | DOCUMENTED | Consumers must inspect JSON status/module entries, not HTTP status alone. Keep behavior unless CD/readiness contract is explicitly changed. |
| AITOS-ENG-008 | P2 | Position-lifecycle API is not durable history | `_RECENT_EVENTS` is a process-local deque capped at 2,000; restart clears it and old events roll off | VERIFIED (source; documented limitation) | DOCUMENTED | Use durable structured logs/journal for historical forensics; do not treat absent event as proof no HOLD/exit occurred. |
| AITOS-ENG-009 | P2 | Data-flow registry retains per-symbol buckets for the process lifetime | `_buckets` is a dictionary keyed by `(stage, symbol)`; latency deques are bounded per bucket but no bucket eviction/TTL is present in the inspected registry | VERIFIED (source); memory impact is a HYPOTHESIS until symbol churn is measured | OPEN / MEASURE | Measure bucket count/RSS under dynamic-universe rotation. If growth is material, design bounded LRU/TTL with safeguards for open-position symbols and aggregate buckets; do not add eviction without regression tests. |
 
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
