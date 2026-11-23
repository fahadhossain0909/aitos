# GitHub Actions Workflow Registry

**Baseline:** `main` at `fbdac28e6eed6623845d84ef5359e965713a3275`  
**Inventory:** 49 YAML workflow files under `.github/workflows/` (directory listing verified 2026-11-23).

This is an inventory, not a claim that all workflows are redundant or safe to delete.
Each workflow must be reviewed for triggers, permissions, environment protection,
secrets, destructive operations, concurrency, and external side effects before
consolidation. The registry intentionally does not merge or disable workflows.

## Core quality and delivery

- `ci.yml` — repository CI matrix and aggregate result.
- `auto-fix.yml` — now read-only formatting/lint verification.
- `console-quality.yml`, `operator-quality.yml` — console/operator UI checks.
- `tauri-desktop.yml`, `linux-console.yml`, `windows-console.yml`,
  `windows-console-sign.yml` — desktop builds/signing.
- `native-rust.yml` — native/Rust checks.
- `backtest.yml`, `strategy-backtest.yml`, `strategy-backtest-suite.yml`,
  `historical-hedge-backtest.yml` — backtest workflows.

## Deployment and runtime operations

- `cd.yml`, `staging-cd.yml` — production/staging deployment.
- `paper-canary.yml`, `paper-service-soak.yml`, `vps-stability.yml`,
  `runtime-hardening.yml`, `maintenance.yml` — runtime operations/acceptance.
- `database-recovery.yml`, `deployment-lock-recovery.yml`,
  `lifecycle-runtime-reconcile.yml` — recovery/reconciliation.
- `reset-paper-data.yml`, `vps-hard-reset.yml` — destructive reset workflows;
  keep manually gated and review exact safeguards before use.

## Persistence, database and security

- `isolated-redis-recovery.yml`, `ledger-quality.yml`,
  `persistence-quality.yml`, `postgres-storage-integration.yml`,
  `three-store-recovery-integration.yml`, `three-store-recovery-ci.yml` — isolated storage/recovery tests; the latter is PR-triggered with read-only permissions and a local venue fixture.
- `security-intent-quality.yml`, `production-audit.yml`,
  `root-cause-forensics.yml` — security/audit evidence.

## Market-data, execution and strategy diagnostics

- `binance-ws-transport-diagnostic.yml`, `market-data-contract-audit.yml`,
  `market-data-duplicate-forensics.yml`, `market-data-e2e-forensics.yml`,
  `market-data-smoke.yml`, `market-data-throughput-benchmark.yml`,
  `production-market-data-forensics.yml` — market-data checks.
- `consumer-identity-forensics.yml`, `integration-flake-forensics.yml`,
  `live-freshness-position-forensics.yml`, `orderbook-targeted-forensics.yml`,
  `trade-path-diagnostic.yml` — targeted forensics.
- `strategy-funnel-position-intelligence-audit.yml`,
  `strategy-performance-audit.yml`, `strategy-trade-lifecycle-audit.yml` —
  strategy/lifecycle diagnostics.

## Consolidation policy

1. Keep one authoritative full CI workflow; specialized workflows should either be
   called by it or have a clearly documented independent purpose and trigger.
2. Any workflow with `contents: write`, `actions: write`, `packages: write`,
   deployment environments, SSH secrets, or destructive commands requires explicit
   owner, scope and approval review.
3. Reset/hard-reset workflows must remain separate from routine CI and require
   manual dispatch plus explicit environment protections; never auto-trigger them
   from a PR or routine push.
4. Consolidate only after verifying branch protection, required-check names,
   downstream dispatch references, artifacts, secrets and operator runbooks.
5. Archive superseded workflows only after inbound references and historical
   evidence are preserved. Do not delete a workflow based on filename alone.

## Static audit tooling

`scripts/audit_workflow_governance.py` now scans every YAML workflow and emits a
per-file JSON/Markdown report. The main CI format job publishes this report as the
`workflow-governance-audit` artifact. It flags implicit permissions, write scopes,
untrusted event expressions, secret/environment patterns, unpinned actions and
high-impact shell commands. These are heuristic flags: they require manual
adjudication and do not prove YAML semantics or effective GitHub policy.

## Next inventory pass

For each file, record exact `on:` triggers, top-level/job-level permissions,
environment names, secret references, destructive shell commands, concurrency
group, called workflows and required-check usage. The directory count and
classification above are verified; the automated per-file static scan is now wired
into CI, while human adjudication, effective permissions/environment checks and
safe consolidation decisions remain pending.
