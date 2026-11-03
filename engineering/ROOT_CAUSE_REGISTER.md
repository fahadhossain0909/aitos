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

| AITOS-ENG-004 | P2 | Data-flow latency exporter may publish 0 for stages with no latency observations | `_append_data_flow_metrics()` converts missing p50/p95/p99 values to numeric zero | ROOT CAUSE CONFIRMED (source semantics); runtime impact unverified | OPEN | `aitos/health_server.py`; `DataFlowRegistry.snapshot_stage()` returns None when sample deque is empty. Existing `tests/forensics/test_data_flow_observability.py::test_health_metrics_match_dashboard_contract` checks metric-name presence, not missing-sample semantics. Propose omitting unobserved latency series or adding explicit sample-count/availability metrics, then update dashboard/tests. Review alert/panel impact before changing output. |
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
