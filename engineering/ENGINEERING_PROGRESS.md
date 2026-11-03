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

## Progress
| Workstream | Status | Evidence / notes |
|---|---|---|
| Repository and open PR discovery | DONE | GitHub branch, commit and PR search |
| Existing agent instructions review | DONE | `skills/aitos-skill.md` and `skills/aitos-prompt.md` exist; root AGENTS.md absent |
| Root agent contract | IN PROGRESS | Added on this branch |
| Engineering operating model | IN PROGRESS | Added on this branch |
| Architecture/data-flow documents | IN PROGRESS | Initial logical map; source/runtime confirmation remains |
| Module inventory | IN PROGRESS | Initial README-backed inventory |
| Telemetry specification | IN PROGRESS | Proposed coverage; existing metrics must be audited |
| Latency baseline | NOT MEASURED | No runtime session or histogram export inspected |
| Root-cause register | IN PROGRESS | Documentation findings only; no runtime defect asserted |
| Runtime audit | NOT STARTED | Requires safe local/isolated environment |
| Automated tests / CI | NOT RUN | Documentation-only changes; branch CI pending |
| Independent review | NOT STARTED | Must review final diff and CI |
| Merge to main | NOT AUTHORIZED | Keep branch/PR isolated until explicit approval |

## Safety constraints carried forward
- Local-first testing. Do not deploy to VPS or resume paused VPS workflows.
- Do not change Redis CPU, pool, or concurrency tuning.
- Do not modify trading strategy semantics or risk parameters without approval.
- Never send live orders.
- Preserve main; do not force-push or force-merge.
- Require CI and appropriate runtime evidence before merge.

## Next actions
1. Finish documentation commits and open a draft PR.
2. Review the complete diff and validate Markdown/links.
3. Run repository CI on the branch.
4. Perform read-only source discovery for module inventory and actual telemetry names.
5. Run safe local paper-runtime diagnostics only when an execution environment is available.
6. Populate latency baseline from measured histograms; never estimate.
7. Have an independent reviewer inspect docs, code references, CI and safety boundaries.
