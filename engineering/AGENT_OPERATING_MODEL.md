# AITOS Agent Operating Model

## Mission
Operate AITOS as an integrated, event-driven quantitative trading system. The objective is safe correctness and evidence-backed reliability—not merely compiling code or starting processes.

## Staged engineering workflow

### Stage 0 — Safety and state capture
- Inspect branch, HEAD, working tree, recent commits, open PRs, and active workflows.
- Preserve unrelated modifications.
- Confirm paper/test/live mode before any runtime action.
- Identify whether positions or monitoring could be interrupted.
- Do not restart, deploy, or mutate persistent state as a diagnostic shortcut.

### Stage 1 — Architecture discovery (read-only)
- Inventory entrypoints, packages, modules, workers, adapters, stores, queues, configuration, tests, Docker services, workflows, and dashboards.
- Trace startup order, dependency edges, event contracts, ownership of state, and shutdown behavior.
- Mark each assertion VERIFIED, HYPOTHESIS, or UNKNOWN.
- Produce architecture and module maps from source, not assumptions.

### Stage 2 — Runtime and observability audit (read-only first)
- Inspect process/service state, health endpoints, metrics, logs, Redis/ClickHouse state, exchange stream freshness, consumer progress, scanner activity, and position reconciliation when the environment is available.
- Separate process liveness, module health, data-plane health, and trading correctness.
- Never claim runtime health from static code inspection.

### Stage 3 — Latency and capacity baseline
- Define stage boundaries and clock semantics before measuring.
- Collect event counts and p50/p95/p99/max only from sufficient samples.
- Report sample count, time window, workload, environment, clock source, and missing stages.
- If metrics do not exist, mark NOT MEASURED and propose instrumentation; do not estimate values.

### Stage 4 — Quantitative decision and lifecycle audit
- Trace candidate discovery, rotation/ranking, features, entry acceptance/rejection, risk checks, order request/ack/fills, persistence, monitoring, HOLD/exit decisions, close attempts/confirmations, and final reconciliation.
- Separate strategy defects from stale/invalid data, infrastructure latency, and execution effects.
- Do not tune strategy or risk behavior without explicit approval.

### Stage 5 — Root-cause register and remediation plan
- Record symptom, first divergence, evidence, root cause confidence, severity, affected components, alternatives considered, fix, regression risks, approval requirement, and rollback.
- Prioritize P0 safety/data-integrity issues, then P1 core pipeline/lifecycle, P2 performance/degraded intelligence, P3 maintainability.

### Stage 6 — Controlled implementation
- Work on an isolated branch/worktree.
- Make the smallest complete change.
- Add focused regression tests.
- Do not bundle unrelated refactors.
- Require explicit approval for strategy, risk, execution, persistence, event-contract, subscription, concurrency, or deployment changes.

### Stage 7 — Verification and independent review
- Run targeted tests, related integration tests, static checks, and broader regression tests when feasible.
- Verify actual runtime behavior in isolated paper mode when possible.
- Have a reviewer who did not author the change inspect correctness, safety, test gaps, and telemetry.
- Report commands, exit codes, test counts, evidence, and unexecuted checks.

### Stage 8 — Durable handoff
- Update progress, root-cause register, latency baseline, module inventory, and verification report.
- Include branch, commit, changed files, PR link, blockers, and exact next action.
- Never call a task RESOLVED if runtime verification is still missing.

## Specialist roles
These are logical responsibilities; use separate agents only when the platform actually supports them.

| Role | Scope | Required output |
|---|---|---|
| Chief Engineering Agent | Priorities, dependencies, approvals, integration | Master plan and progress |
| Architecture Agent | Modules, contracts, startup, dependencies | Architecture and inventory |
| Runtime/SRE Agent | Processes, readiness, recovery, incidents | Runtime evidence and root causes |
| Data Pipeline Agent | Exchange, parsing, Redis, ClickHouse, consumers | Data-flow and freshness report |
| Performance Agent | Stage latency, queues, CPU/RAM, throughput | Reproducible baseline and bottlenecks |
| Quant Research Agent | Candidate/entry/exit decision quality | Decision attribution and outcome analysis |
| Execution/Lifecycle Agent | Orders, fills, persistence, positions | Lifecycle and reconciliation report |
| Observability Agent | Metrics, logs, traces, dashboards | Telemetry coverage and gaps |
| QA Agent | Unit/integration/contract/recovery tests | Test matrix and results |
| Independent Reviewer | Diff and evidence review | Findings; no self-approval |

## Parallel-work rules
- Parallelize independent read-only discovery and test design.
- Give each implementation agent an isolated branch/worktree or non-overlapping file ownership.
- Do not let agents concurrently edit the same files or shared configuration.
- The Chief Agent integrates changes and resolves conflicting assumptions.
- The independent reviewer must inspect the final diff and evidence, not just the author's summary.

## Runtime and monitoring truthfulness
A periodic script, scheduled task, agent session, and supervised 24/7 service are different guarantees. State exactly which exists. Continuous monitoring requires a durable execution host, supervision/restart policy, bounded polling, checkpoints, incident deduplication, and a tested notification route. Do not claim continuous monitoring from a one-time run.

## Required report format
1. Executive summary and operating mode.
2. Module health matrix (expected / actual / evidence / status).
3. Root-cause register (severity / confidence / affected modules / fix).
4. Latency report (stage / p50 / p95 / p99 / max / sample count / status).
5. Market-data and event-bus health.
6. Candidate and trading decision flow.
7. Order and position lifecycle/reconciliation.
8. Telemetry coverage and dashboard verification.
9. Changes and exact files.
10. Commands, tests, runtime evidence, and limitations.
11. Remaining risks, rollback, and next steps.
