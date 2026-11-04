# AITOS Agent Engineering Contract

This file is the repository-level entry point for every AI coding agent.

## Required reading
1. Read `skills/aitos-skill.md` before investigating or changing the system. It defines the detailed reliability, safety, autonomy, incident, and verification protocol.
2. Read `engineering/AGENT_OPERATING_MODEL.md` for the staged workflow, specialist roles, and evidence requirements.
3. Read the relevant architecture, data-flow, telemetry, and progress documents before making changes in those areas.
4. Read `README.md` and the relevant implementation/tests. Source code and observed runtime behavior take precedence over historical documentation.

If instructions conflict, follow the stricter safety rule. This file and `skills/aitos-skill.md` take precedence over older prompt templates such as `skills/aitos-prompt.md`.

## Non-negotiable rules
- Understand call paths, contracts, dependencies, startup/shutdown, and paper/live effects before editing.
- Distinguish verified facts, reproducible failures, supported root causes, hypotheses, and unknowns.
- Never fabricate runtime state, metrics, test results, latency values, or completion claims.
- Measure before optimizing; reproduce before fixing; fix root causes rather than symptoms.
- Preserve user changes and unrelated work. Inspect Git status and branch before acting.
- Use an isolated branch/worktree for maintenance. Do not force-push or rewrite shared history.
- Do not merge into `main` without explicit authorization and required CI/review/operational gates.
- Never submit real exchange orders. Never activate live trading.
- Do not deploy to a VPS or resume paused VPS-related workflows. Local-first validation remains the policy.
- Do not delete data, run destructive migrations, reset databases, or perform destructive Git cleanup.
- Do not change strategy semantics, entry/exit thresholds, position sizing, leverage, risk limits, or production configuration without explicit approval.
- Architecture-sensitive changes (subscriptions, Redis consumer behavior, event schemas, persistence/recovery, concurrency, queue limits, execution coordination) require explicit approval before activation/deployment.
- Do not change Redis CPU, connection-pool, or concurrency settings without explicit approval.
- A process being alive, a health endpoint returning 200, or a metric being exposed does not prove end-to-end functionality.
- Extend existing telemetry and configuration mechanisms; avoid duplicate frameworks and high-cardinality metric labels.
- Never log secrets, API keys, tokens, or unnecessary personal/account data.

## Required workflow
1. Establish repository/branch/commit/status and preserve local changes.
2. Discover the actual architecture and relevant runtime mode.
3. Create an evidence-backed baseline; label unavailable measurements NOT MEASURED.
4. Audit startup, data flow, event transport, scanner, trading lifecycle, execution, persistence, and observability as applicable.
5. Reproduce and register issues with severity and confidence.
6. Propose the smallest safe fix and identify downstream/regression risks.
7. Obtain approval for changes covered by the restrictions above.
8. Implement on an isolated branch, add regression tests, run targeted then broader checks.
9. Review the complete diff and verify runtime behavior only in a safe isolated/paper environment.
10. Update engineering progress, root-cause register, verification report, and rollback notes.
11. Open a draft PR when useful; do not merge without explicit authorization.

## Evidence labels
Use exactly these labels in reports:
- VERIFIED — supported by source, test, runtime observation, or reproducible evidence.
- REPRODUCED — failure observed by executing a repeatable case.
- ROOT CAUSE CONFIRMED — causal mechanism demonstrated, not merely suspected.
- HYPOTHESIS — plausible explanation awaiting evidence.
- NOT MEASURED — measurement not collected.
- NOT TESTED — test not executed.
- BLOCKED — required environment, permission, or dependency unavailable.

## Completion standard
A task is complete only when the change, tests, runtime evidence, limitations, and remaining risks are documented. A code edit or passing unit test alone is not end-to-end verification.
