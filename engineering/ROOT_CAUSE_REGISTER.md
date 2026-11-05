# AITOS Root-Cause Register

## Register rules
- One row per distinct incident or reproducible defect.
- Do not promote a hypothesis to root cause without causal evidence.
- Link evidence, logs, traces, tests, commits and PRs where available.
- Keep resolved incidents with their verification evidence; do not erase history.

## Initial register
| ID | Severity | Symptom | Root cause | Confidence | Status | Evidence / next action |
|---|---|---|---|---|---|---|
| AITOS-ENG-001 | — | Engineering operating model and durable docs were not present at repository root | Documentation gap; no runtime defect implied | VERIFIED (repository inspection) | COMPLETE IN MAIN | `engineering/AGENT_OPERATING_MODEL.md` and `skills/aitos-skill.md` are present in main and define the agent workflow and repository-specific safety contract. |
| AITOS-ENG-002 | — | Runtime/latency baseline not yet captured by this work | Environment/runtime not accessed in this documentation change | VERIFIED (scope limitation) | NOT MEASURED | Run isolated paper-runtime audit and populate baseline |
| AITOS-ENG-003 | — | Open testnet integration PRs may overlap | PRs #210 and #213 are closed; PR #215 remains open and has overlapping Binance Futures Testnet scope | VERIFIED (GitHub PR search) | OPEN — REVIEW PR #215 SEPARATELY | Do not merge the testnet PR into the P-0/P-1/P-2 remediation set without a dedicated diff/CI/runtime review. |
| AITOS-ENG-004 | P2 | Data-flow latency exporter may publish 0 for stages with no latency observations | `_append_data_flow_metrics()` previously converted missing p50/p95/p99 values to numeric zero | VERIFIED (source and PR #217 merged) | RESOLVED IN MAIN — RUNTIME NOT MEASURED | PR #217 was merged. Unobserved percentile series are omitted, `aitos_data_flow_latency_sample_count` is exported, and genuine 0.0 ms values remain distinguishable from missing samples. Runtime scrape validation remains part of the combined runtime pass. |
| AITOS-ENG-005 | P2 | Startup failure during paper-runner initialization may bypass graceful cleanup | The main runtime `try/finally` began after Redis, ClickHouse, system modules, recovery, and auxiliary agents had already been initialized | VERIFIED (control-flow inspection) | FIX IMPLEMENTED — NOT VERIFIED | `run_paper_trading.py`: wrap startup in `AsyncExitStack`, register cleanup immediately after each resource is acquired, disarm the startup stack only after all startup wiring succeeds, and retain the existing runtime `finally` as the normal teardown owner. `TradeStatePersistence.initialize()` now retains each successful subscription incrementally so partial initialization can be rolled back. Added failure-injection tests for ClickHouse startup failure and partial subscription registration. CI/runtime pending. |
| AITOS-ENG-006 | P2 | Self-heal coverage differed from health-report coverage | HealthServer received core modules plus recorder, MarketOS persistence and four agents; periodic self-heal loop iterated only `components.all_modules()` | VERIFIED (source) | FIX IMPLEMENTED — NOT VERIFIED | Added `restart_unhealthy_modules()` for explicitly owned auxiliary modules; paper self-heal now includes MarketOS persistence, LearningExperienceRecorder and the four AI agents. Their shutdown paths now reset initialized state; recorder and MarketOS subscriptions are registered incrementally and rolled back on partial failure; MarketOS health now reports a dead flush task as unhealthy. Added restart-order and module reinitialization tests. CI pending; runtime health recovery remains deferred. |
| AITOS-ENG-007 | P2 | HTTP-only health probes may miss degraded module state | `/health` intentionally returns HTTP 200 when any module is DEGRADED; payload reports `status=degraded` | VERIFIED (source; intentional behavior) | DOCUMENTED | Consumers must inspect JSON status/module entries, not HTTP status alone. Keep behavior unless CD/readiness contract is explicitly changed. |
| AITOS-ENG-008 | P2 | Position-lifecycle API is not durable history | `_RECENT_EVENTS` is a process-local deque capped at 2,000; restart clears it and old events roll off | VERIFIED (source; documented limitation) | DOCUMENTED | Use durable structured logs/journal for historical forensics; do not treat absent event as proof no HOLD/exit occurred. |
| AITOS-ENG-010 | P0 | Trading startup could continue after restored positions failed market-feed resubscription | Paper runner ignored the resubscription verification result; live runner restored positions but did not perform the explicit post-initialization resubscription/verification step | VERIFIED (source control-flow inspection) | FIX IMPLEMENTED — NOT VERIFIED | `run_paper_trading.py`, `run_live_trading.py`, `TradeStatePersistence.resubscribe_restored_positions()`. Added a shared fail-closed readiness gate: if any restored position exists, every ingestion instance must verify both trade and kline symbol coverage; otherwise the scan loop is skipped and managed shutdown runs. Regression tests cover empty portfolio, missing feeds, partial verification, failures and malformed evidence. GitHub CI pending; runtime verification intentionally deferred. |
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
