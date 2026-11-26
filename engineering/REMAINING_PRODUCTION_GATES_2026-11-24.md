# AITOS PR #263 — implementation and production acceptance status

Updated 2026-11-26. This document separates code delivered on the branch from
actions that require the user's production infrastructure, credentials,
operator approval, or authenticated venue access. A passing CI run is not a
production deployment authorization.

## Code implemented on this branch

### Canonical PostgreSQL OMS and execution authority

- Scope-keyed PostgreSQL canonical OMS with aggregate revision CAS, writer
  generation fencing, idempotent event identities, hash-chain verification,
  causation/correlation/parent-event metadata, and immutable financial ledger
  entries.
- PostgreSQL canonical execution-intent store is bound into the live Binance
  executor. The live runner does not construct the legacy SQLite intent store.
- TradeLifecycle is bound to the PostgreSQL-first lifecycle writer. Confirmed
  entry fills, reductions, partial closes, full exits and protective child fills
  persist their trade snapshot, execution-intent transition, financial journal
  entries and financial-state revision through the canonical PostgreSQL path.
- PostgreSQL financial ledger uses balanced per-currency double-entry postings.
  Fee/funding reconciliation is idempotent by Binance income transaction ID;
  startup reconciles realized/unrealized PnL, fees, funding, equity, peak equity
  and drawdown against authenticated venue state.
- The live runner fails closed or enters manage-only mode when writer fencing,
  unresolved intents, position reconciliation, income attribution, financial
  reconciliation or projection replay is incomplete.
- The canonical outbox uses leased claims, owner/token fencing, expiry/reclaim,
  bounded retries, replay, parity audit and explicit rebuild support.

### ClickHouse scoped-state cutover

- The migration tool creates additive scope-first plain MergeTree tables,
  fingerprints each source occurrence, retains the exact source payload,
  verifies payload hashes and supports explicit backup-preserving cutover and
  verified rollback.
- Live and paper runtime startup now require both active state tables to have
  the complete scope-keyed schema, a plain MergeTree engine and
  `account_scope` as the first sorting-key field. They do not silently create
  or fall back to the unsafe legacy ReplacingMergeTree layout.
- Runtime writes to cutover tables include account scope, exact source
  fingerprint, physical occurrence number and canonical payload hash.
  Closed-trade archival remains append-only; runtime deletion requires an
  existing closed archive and does not mutate historical rows.
- ClickHouse-to-PostgreSQL import compares physical row occurrences rather
  than ReplacingMergeTree FINAL counts, quarantines ambiguous same-timestamp
  snapshots, preserves immutable source evidence and is resumable.
- The migration/import scripts are not executed against production data by
  this PR. Historical versions already collapsed by an earlier ReplacingMergeTree
  merge cannot be reconstructed.

### Protection and recovery

- Durable execution-intent preclaims, deterministic client IDs, POST-to-ID
  recovery, make-before-break protection handovers, triggered algo-to-child
  association, cumulative/partial child-fill markers, fee/funding attribution
  and three-store crash/restart fixture coverage are implemented.
- The three-store CI fixture uses disposable ClickHouse and SQLite plus a local
  signed venue protocol fixture. It does not submit Binance orders.

### Identity and live approval

- WebAuthn relying-party code verifies user-bound registration and assertions,
  exact origin/RP binding, user verification, challenge expiry/consumption,
  credential counters, revocation and audit.
- Operator gateway enforces identity-bound sessions, MFA, server-side RBAC,
  CSRF/origin boundaries and action-bound approval.
- Desktop includes WebAuthn ceremonies and an Approval Desk. The executor
  requires a trusted approval authority and consumes a short-lived, single-use
  signed receipt bound to the exact order action. Mainnet remains fail-closed
  unless the shared production authority is configured.

### CI and repository controls

- Compose policy, digest-pinned runtime artifacts, workflow permission
  inventory, secret-safe automation, Python 3.10 compatibility, lint/security
  checks, PostgreSQL integration and isolated recovery workflows are present.
- No historical workflow has been deleted. Static workflow inventory is not
  human security adjudication.

## Production-only acceptance gates — not performed by this branch

1. **ClickHouse migration and cutover:** stop every writer, create and restore-
   test a backup, review every account-scope attribution, run the migration
   with explicit `--apply --cutover` acknowledgements, verify source/target
   physical counts and hashes, and confirm active tables and retained backups.
2. **PostgreSQL production migration:** apply the reviewed migrations to the
   production database, verify backup/restore, writer-fence behavior and
   multi-process failover. No production database was modified.
3. **Authenticated Binance Demo/Testnet drill:** verify entry submit/response
   loss/client-ID recovery, protection POST-to-ID, algo-child linkage,
   cumulative and partial fills, partial/full exits, fees/funding and repeated
   process restarts. The local signed fixture is not venue acceptance.
4. **WebAuthn deployment acceptance:** configure the external HTTPS origin,
   RP ID, credential database, signing key and server-owned role map; enroll
   real hardware authenticators and test revocation, backup credentials and
   single-use approval receipts through the deployed desktop/gateway/executor.
5. **Persistent three-store fault injection:** repeat crash/restart and
   ambiguous-state drills against the authorized persistent services and
   venue test account; verify no duplicate orders, lost fills or scope bleed.
6. **Workflow security review:** a human must adjudicate every workflow's
   triggers, permissions, secrets/environments, external side effects,
   destructive commands, action pinning and required-check dependencies.
7. **Production operations:** configure secrets, TLS, backups, alerting,
   resource limits, retention, monitoring and rollback ownership; complete a
   supervised canary before enabling any new exposure.

## Release decision

The branch is a code candidate, not yet an approved production release. Keep
the PR Draft and Mainnet disabled until exact-head CI passes, the ClickHouse
cutover is completed and verified, production PostgreSQL migration/restore is
accepted, authenticated Binance testnet recovery passes, and WebAuthn operator
approval is proven in the deployed environment. Never infer those outcomes
from source code or simulated fixtures.
