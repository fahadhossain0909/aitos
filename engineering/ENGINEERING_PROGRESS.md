# AITOS Engineering Progress

## Current initiative
Establish a reusable, evidence-driven engineering operating model for any AI coding agent working on AITOS.

## Repository baseline
- Repository: `fahadhossain0909/aitos`
- Base branch: `main`
- Base commit inspected: `b9d4a60a5ac1989455837fec277e2977d520de5e`
- Work branch: `feat/engineering-agent-operating-system`
- Baseline date: 2026-11-03
- Runtime environment: NOT ACCESSED
- Trading mode: NOT VERIFIED
- Open overlapping testnet PRs observed: #210, #213, #215

## Pull request
- Draft PR: https://github.com/fahadhossain0909/aitos/pull/216
- Current PR #216 head: `94dd1f7ece1ffc9119c85d8c8cad1c94181ee211`
- PR #216 changed files: 10 (documentation-only).
- Related implementation PR #217 head: `c1730d7730c42037bc804fe056f8c782e15744d0` (6 code/test/dashboard files).
- GitHub Actions PR workflow runs observed at initial check: none; combined status checks: none. CI is PENDING, not passed.

## Progress
| Workstream | Status | Evidence / notes |
|---|---|---|
| Repository and open PR discovery | DONE | GitHub branch, commit and PR search |
| Existing agent instructions review | DONE | `skills/aitos-skill.md` and `skills/aitos-prompt.md` exist; root AGENTS.md absent |
| Root agent contract | DONE | Added on this branch |
| Engineering operating model | DONE | Added on this branch |
| Architecture/data-flow documents | DONE (initial map; runtime confirmation pending) | Initial logical map; source/runtime confirmation remains |
| Module inventory | PARTIAL | README and paper-runner/health source-level inventory; remaining modules not yet inspected |
| Telemetry specification | DONE | Specification and source audit documented; targeted code fix split into PR #217 for fix-branch policy |
| Latency baseline | NOT MEASURED | No runtime session or histogram export inspected |
| Root-cause register | PARTIAL | 9 source/documentation findings recorded; runtime root causes remain unverified |
| Runtime audit | BLOCKED / NOT STARTED | No connected local runtime or authorized isolated test environment in this session |
| Automated tests / CI | PASS (GitHub Actions) | PR #217: 984 tests pass on Python 3.10/3.11/3.12; market-data audit, backtest, formatting, lint, security and Docker/Compose checks pass. PR #216 CI passed on an earlier docs-only head; later changes were docs-only. Local runtime remains untested. |
| Independent review | NOT STARTED | Must review final diff and CI |
| Merge to main | NOT AUTHORIZED | Keep branch/PR isolated until explicit approval |

## Safety constraints carried forward
- Local-first testing. Do not deploy to VPS or resume paused VPS workflows.
- Do not change Redis CPU, pool, or concurrency tuning.
- Do not modify trading strategy semantics or risk parameters without approval.
- Never send live orders.
- Preserve main; do not force-push or force-merge.
- Require CI and appropriate runtime evidence before merge.

## Source-level audit findings
- Inspected `aitos/health_server.py`, core Event/Health contracts, data-flow registry/schema, Prometheus scrape config, data-flow tests, and Grafana dashboard query references.
- Confirmed AITOS-ENG-004 and moved its code remediation to dedicated `fix/telemetry-missing-sample` / PR #217 to comply with the operations skill's fix-branch rule. CI/runtime verification pending.
- Existing data-flow registry is process-local and retains a bounded 2,048 latency samples per stage/symbol bucket.
- Prometheus scrape interval is configured to 15 seconds for paper/live targets; actual scrape status remains NOT TESTED.
- Paper runner source audit found startup cleanup coverage begins only after a long initialization sequence (AITOS-ENG-005); auxiliary-module self-heal coverage differs from health-report coverage (AITOS-ENG-006); degraded health returns HTTP 200 intentionally (AITOS-ENG-007); position event API is bounded process-local memory (AITOS-ENG-008); per-symbol data-flow buckets have no source-level TTL/eviction (AITOS-ENG-009; memory impact NOT MEASURED).

## Next actions
1. Complete source inventory for exchange, Redis, ClickHouse, execution and risk modules.
2. Run the documented local paper-runtime audit on the user's safe local paper stack; inspect restored-position subscriptions and current open positions.
3. Capture real stage latency distributions and fill the baseline with sample counts/window/mode.
4. Verify decision-to-outcome lifecycle telemetry and Prometheus/Grafana rendering in the local runtime.
5. Investigate AITOS-ENG-005/006/009 with targeted failure-injection and symbol-churn tests before changing lifecycle or retention behavior.
6. Obtain independent human review for PR #216 and PR #217; do not merge without explicit approval.
