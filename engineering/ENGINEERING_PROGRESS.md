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
- Current head at last status check: `b36ae68db55554312a845520870a2da049285327`
- Changed files at last comparison: 15 (documentation + telemetry source/test/dashboard changes)
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
| Automated tests / CI | NOT RUN / NOT OBSERVED | Code changed; no execution environment; GitHub Actions run/status lookup returned none |
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
1. Run `python -m pytest -q tests/forensics/test_data_flow_observability.py` and `black --check` in the repository environment.
2. Run repository CI and resolve any failures.
3. Complete source inventory for exchange, Redis, ClickHouse, scanner, execution, risk and lifecycle modules.
4. Run the documented local paper-runtime audit with Redis/ClickHouse and verify restored-position subscriptions.
5. Capture real stage latency distributions and fill the baseline with sample counts/window/mode.
6. Verify decision-to-outcome lifecycle telemetry and Prometheus/Grafana rendering.
7. Obtain independent review before marking PR #216 ready; do not merge without explicit approval. Track telemetry code verification in PR #217.
