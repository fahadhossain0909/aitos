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

The current active lifecycle still uses multiple stores and the revision journal
prototype remains unwired. AUD-20261106-46/47/48/49 remain OPEN:

- replacement-merge conflict loss needs an active append-only financial authority
  and a non-destructive legacy reconciliation/import plan;
- causal revision and writer fencing must be wired into the active writer topology,
  not merely proven in an isolated SQLite prototype;
- trade identity must be consistently scoped through runtime, ledger, income and
  recovery readers before multi-account/shared-data use;
- the income cursor needs a monotonic high-water mark plus durable unresolved-row
  retry state, not a timestamp-only last-writer-wins value.

The protective POST-to-ID path still requires end-to-end venue acknowledgement,
durable identity, restart and partial/triggered child-fill acceptance across the
active execution path. Existing initial/replacement protection recovery code and
offline tests are not equivalent to a full authenticated venue acceptance.

No automatic financial migration or authority cutover is performed in this branch.

### Phase 4 — Signed approval control plane

The existing live runner still has a terminal confirmation gate. A complete
replacement requires an authenticated desktop/control-plane issuer, WebAuthn or
hardware-backed identity, RBAC policy, signed short-lived approval envelope,
durable single-use/replay protection, audit retention and runtime verification.
The current read-only operator gateway is not an authenticated trading mutation
control plane. No fake identity, shared secret, or terminal-only substitute is
introduced as if it solved this requirement.

### Remaining CI and operational gates

- Full current-head GitHub Actions matrix and all specialized workflows must finish.
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
