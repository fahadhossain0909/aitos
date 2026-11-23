# AITOS Production Readiness Remediation — 2026-11-23

## Scope

This branch implements the approved six-phase remediation plan on top of the current
`main`. It is a code and CI change set only. It does not authorize a merge, hosted
deployment, database migration, testnet order, or live trading activation.

## Implemented in this branch

### Phase 1 — Compose security policy

- Removed the Redis `command` override. Compose replaces command arrays instead of
  merging them; the base service now retains `requirepass`, bounded `maxmemory`,
  `noeviction`, AOF and `appendfsync everysec`.
- Removed the override that raised `DATA_DISK_MIN_FREE_GB` from the base Compose
  threshold of 2.5 GB to 20 GB.
- Added `scripts/validate_compose_policy.py`, which inspects the normalized Compose
  JSON rather than source YAML and asserts the merged Redis/storage policy.
- CI now runs the policy validator against `docker compose config --format json`.

### Phase 2 — Paper/Live artifact parity

- Removed the paper source bind mount over `/app`; paper now executes the built image.
- Paper and Live resolve the same `AITOS_RUNTIME_IMAGE` reference.
- Production and staging image publication expose the pushed BuildKit digest and
  pass `repository@sha256:<digest>` into the deployment environment.
- CD validates digest format and Paper/Live parity before pulling/recreating services.
- The existing deployment opt-in remains unchanged; this branch does not deploy.

### Phase 5 — Runtime compatibility and reconciliation resilience

- Replaced bare `TimeoutError` handlers around `asyncio.wait_for` shutdown waits in
  paper/live entrypoints with `asyncio.TimeoutError`, preserving Python 3.10 behavior.
- Reconciliation failures now set the account entry gate, emit critical structured
  diagnostics, and use bounded exponential backoff. The scheduler does not clear
  the gate automatically.
- Added focused regression tests for Python timeout handling and backoff behavior.

### Phase 6 — CI and automation governance

- Converted the main auto-fix workflow into read-only Black/isort/Ruff checks.
  It no longer writes to `main`, commits bot changes, or needs `contents: write`.
- Removed direct interpolation of commit messages into shell source by removing the
  shell-based auto-fix branch-selection logic.
- CI tests use disposable CI-only values in `.env`; repository secrets are no longer
  written into test files.
- Bandit now includes low-severity findings (`-l`) rather than medium+ only.
- Docker CI validates the merged security-critical Compose model.

## Explicitly open — not claimed as fixed

### Phase 3 — Financial authority and execution recovery
### Account-scope recovery hardening

- Live runtime state is now bound to the durable execution-intent scope; paper state
  uses the explicit `AITOS_PAPER_ACCOUNT_SCOPE` value (default
  `paper:default`). New runtime and closed-ledger payloads retain this scope.
- Open/closed recovery refuses mixed or unexpected account scopes rather than
  loading several accounts into a lifecycle keyed only by trade ID.
- Closed/open anti-join, terminal-history reads, performance reads and runtime
  deletion are scoped to the active store. The close-recovery comparison uses
  `(account_scope, trade_id)`, so a closed trade in another scope cannot suppress
  an open position.
- This is a fail-closed code safeguard, not a physical schema migration. Existing
  unscoped records require an explicit, reviewed account-attribution/import plan
  before switching a runtime to a non-empty scope.
- The legacy ClickHouse `ReplacingMergeTree` still uses `ORDER BY trade_id`.
  Embedded counterexample coverage confirms that a background/final merge can
  collapse distinct same-ID trades across scopes even though the reader query is
  scope-aware. A versioned scope-keyed table, lossless import, verification and
  controlled cutover are still required; this remains an open Phase 3 blocker.


The current active lifecycle still uses multiple stores and the revision journal
prototype remains unwired. AUD-20261106-46/47/48 remain OPEN. The active
`ExecutionIntentStore` now writes immutable, transaction-ordered transition evidence
for order intents, protection handovers and child-fill watermarks using SQLite
triggers in the same transaction as each state mutation. This is a migration bridge
and forensic audit stream—not yet the canonical financial ledger, not a CAS/fenced
multi-writer OMS, and not a replacement for the trade/equity/fee/funding stores. AUD-20261106-49 has
a code-level remediation in this branch (monotonic high-water cursor plus a
separate durable unresolved-income retry table); exact-head CI and real ClickHouse
restart/retry verification are still pending:

- replacement-merge conflict loss needs an active append-only financial authority
  and a non-destructive legacy reconciliation/import plan;
- causal revision and writer fencing must be wired into the active writer topology;
  the new transition sequence provides transaction order for evidence only and does
  not yet prevent stale writers or replace active state projections;
- trade identity must be consistently scoped through runtime, ledger, income and
  recovery readers before multi-account/shared-data use;
- AUD-49: `load_income_cursor` now reads `maxOrNull(cursor_time_ms)` and unresolved
  rows are persisted in an account-scoped retry ledger before the high-water cursor
  advances. Retry-floor reads re-query older unresolved rows without cursor rewind.
  This is code-level implementation, not yet accepted multi-store/ClickHouse
  crash-recovery evidence.

The protective POST-to-ID path still requires end-to-end venue acknowledgement,
durable identity, restart and partial/triggered child-fill acceptance across the
active execution path. Existing initial/replacement protection recovery code and
offline tests are not equivalent to a full authenticated venue acceptance.

No automatic financial migration or authority cutover is performed in this branch.

### Phase 4 — Signed approval control plane

The live kernel now reads `require_human_approval_for_prod` from typed settings
instead of hardcoding `True`, but the runner still has a terminal confirmation
gate that trusts a self-entered operator label. A complete replacement requires
an authenticated desktop/control-plane issuer, WebAuthn or
hardware-backed identity, RBAC policy, signed short-lived approval envelope,
durable single-use/replay protection, audit retention and runtime verification.
The current read-only operator gateway is not an authenticated trading mutation
control plane. No fake identity, shared secret, or terminal-only substitute is
introduced as if it solved this requirement.

### Remaining CI and operational gates

- Exact-head GitHub Actions verification completed successfully: Main CI (Python 3.10, 3.11 and 3.12), Formatting/Lint, Backtest, PostgreSQL Storage Integration, Redis Recovery, Ledger SQL Quality, Market Data Contract Audit and isolated persistence evidence all PASS. Binance WebSocket and Order-book targeted diagnostics were skipped by their workflow conditions; they are not reported as passes.
- A 48-file GitHub Actions inventory and consolidation policy are now in
  `docs/WORKFLOW_REGISTRY.md`; per-workflow trigger/permission/secret/destructive
  review and any safe archive/migration are still pending.
- The static workflow governance audit tool and CI artifact are implemented. The
  48-workflow inventory is not considered a completed human security review until
  each finding is adjudicated and safe consolidation is reviewed.
- Real Compose runtime inspection is NOT MEASURED by source/CI policy alone.
- Production/staging deployment, persistence/restart acceptance, authenticated
  testnet account verification, venue POST-to-ACK fault injection, independent
  security review and live lifecycle acceptance remain NOT TESTED/BLOCKED as
  appropriate.
- Documentation and workflow consolidation still needs a repository-wide inventory
  and safe archive plan; no historical workflow or document is deleted here.

## Rollback

Revert this branch's commits. The branch does not mutate persistent runtime data.
For deployment, the previous commit-tagged image remains addressable; no rollback
or deployment command is executed by this remediation.
