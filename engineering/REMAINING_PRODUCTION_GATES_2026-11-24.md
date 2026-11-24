# Production-readiness implementation status — 2026-11-24

This document records code additions on PR #263 and explicitly distinguishes
implemented primitives from integrated production capabilities. Nothing here
authorizes a database cutover, deployment, testnet order or mainnet execution.

## New code on this branch

### Scope-keyed ClickHouse migration
- `scripts/migrate_scoped_clickhouse_state.py` stages exact legacy payloads in
  additive MergeTree v2 tables with account-scope ordering, source fingerprints,
  occurrence numbers and payload hashes.
- Legacy rows without embedded scope require an explicit fingerprint-to-scope
  attribution manifest. Conflicts and ambiguous attribution fail closed.
- The importer requires explicit writer-freeze and backup acknowledgements.
- It has not been run against production data. Runtime readers/writers still
  use the legacy tables. Historical versions already collapsed by
  ReplacingMergeTree cannot be reconstructed from the current source table.
- Therefore schema creation, import, source/target reconciliation, and reader/
  writer cutover remain an operational acceptance gate, not a completed migration.

### Transactional OMS primitive
- `aitos/execution/oms_journal.py` adds a scope-keyed append-only SQLite WAL
  event journal with writer-generation fencing, aggregate revision CAS,
  idempotent event IDs, hash chaining and a projection outbox.
- Unit tests cover stale writer rejection, revision conflicts, scope isolation,
  idempotency and append-only constraints.
- This is explicitly a single-host paper/test primitive. It is NOT yet wired
  into the live `ExecutionIntentStore` / TradeLifecycle transaction boundary
  and is NOT the canonical financial authority. A PostgreSQL transactional
  implementation, migration of existing financial state, atomic lifecycle
  integration, and restart/HA acceptance remain required.

### Live approval boundary
- `aitos/security/live_approval.py` adds challenge/action binding, role checks,
  short-lived HMAC-signed approval tokens, one-time durable consumption and an
  append-only audit table.
- Mainnet `BinanceFuturesOrderExecutor` construction now requires an injected
  approval consumer; each mainnet order consumes an approval bound to the
  exact environment, scope, client ID, symbol, side, quantity, price, order
  type, reduce-only flag and owner trade.
- The module deliberately delegates WebAuthn cryptographic assertion
  verification to a relying-party adapter. A real adapter with credential
  registration/storage, clientDataJSON, challenge, origin, RP-ID hash,
  authenticator flags, signature/counter verification, MFA policy and
  authenticated RBAC identity is NOT yet supplied or integrated with the
  desktop control plane. The HMAC key must be held by a protected service,
  not the desktop client.
- Therefore these primitives do not establish production WebAuthn readiness.
  Mainnet must remain disabled unless the deployment injects a trusted,
  independently verified RP/identity adapter and protected approval service.

### Protective order and three-store recovery
- The branch already contains durable intent preclaims, protection handover
  state, algo-to-child linkage validation, cumulative child-fill watermarks,
  and a read-only CI workflow using real disposable ClickHouse + SQLite and a
  local signed venue protocol fixture.
- The fixture is not Binance. It verifies deterministic recovery mechanics
  and never sends exchange orders. Authenticated demo-testnet read-only
  acceptance remains a separate opt-in gate and may be blocked by runner
  egress restrictions.
- No mainnet order or external testnet write has been executed.

## Current status by required workstream

| Workstream | Code status | Production acceptance |
|---|---|---|
| ClickHouse scope-keyed migration | Additive importer + tests | Not run; import/cutover outstanding |
| Canonical OMS / CAS / fencing | Standalone primitive + tests | Not wired to active lifecycle; PG/HA outstanding |
| Protective POST-to-ID and algo child fill recovery | Durable recovery path + tests | Isolated fixture acceptance pending/current-head CI |
| Venue + SQLite + ClickHouse restarts | Fault-injection workflow + tests | CI results and authenticated venue proof required |
| WebAuthn / hardware approval | Token/RBAC/replay primitive + mainnet hook | Real RP adapter, identity/RBAC, desktop integration outstanding |

## Safety decision

Do not declare AITOS production ready. Do not enable mainnet. Do not run the
ClickHouse importer until the writer freeze, backup, account attribution
manifest and pre/post count/hash reconciliation are reviewed. Do not claim
WebAuthn acceptance until a real relying-party verifier is deployed and
hardware-authenticator tests pass. Do not switch the active OMS authority until
the live lifecycle writes and reads through the same transactional store.


## Follow-on implementation update — PostgreSQL OMS

A PostgreSQL-native canonical OMS journal is now present in
`aitos/execution/postgres_oms.py`, with schema migration v2 in
`aitos/storage/postgres_migrations.py`. It implements scope-level writer
generation CAS/fencing, aggregate revision CAS, idempotent event IDs,
canonical-payload hash chaining, and a transactional projection outbox.
The PostgreSQL storage integration workflow now invokes
`tests/storage/test_postgres_oms.py` against its disposable PostgreSQL service.

This is a database-backed OMS primitive, not yet the active lifecycle authority.
TradeLifecycle, ExecutionIntentStore, fee/funding accounting, position/equity
state and startup hydration have not all been converted into one PostgreSQL
transaction boundary. Do not switch runtime authority or claim canonical OMS
acceptance until that integration and multi-process/restart tests pass.

## Workflow adjudication count correction

The checked-out PR head contains **49** workflow YAML files under
`.github/workflows/`, not 48. The existing registry is the 49-file inventory.
Static findings are triage signals, not human security approval. No workflow
has been deleted or disabled in this increment: safe consolidation requires
per-file review of event trust, permissions, secrets/environments, pinned
actions, external side effects, destructive commands, required-check names,
and dispatch/call dependencies. The reset and hard-reset workflows remain
separate and must not be folded into routine CI.


## Follow-on implementation — 2026-11-24

The following code-level increments are now present on this draft branch:

- `aitos/execution/postgres_lifecycle_writer.py` adds a PostgreSQL-first
  TradeLifecycle state-writer adapter. It commits a scope-fenced aggregate
  revision before calling the existing SQLite/ClickHouse projection writer,
  and exposes `latest_snapshot()` for canonical snapshot recovery.
  PostgreSQL integration coverage verifies that projection failure does not
  roll back the canonical event.
- `scripts/migrate_scoped_clickhouse_state.py` now has an explicit
  `--cutover` path. It requires `--apply`, explicit writer-stopped and
  restorable-backup confirmations, an Atomic database, absent retained-backup
  names, row-count parity, and retains both legacy tables under
  `*_legacy_pre_scope_v1`. Unit tests cover the Atomic/backup guards.
- `aitos/security/webauthn_rp.py` adds a py_webauthn-backed RP implementation:
  registration options, one-time user-bound registration challenges,
  cryptographic attestation verification, persistent public-key credentials,
  authenticator user-verification enforcement, signature verification and
  sign-counter compare-and-update. `requirements.txt` now declares
  `webauthn>=2.2.0,<3.0.0`.

These are implementation components, not acceptance claims. In particular:

1. The PostgreSQL writer adapter has **not yet been installed by the production
   application composition root**. The runtime must acquire a fresh writer
   fence on startup, bind this adapter after configuring the durable projection
   callback, hydrate open trades from canonical snapshots before enabling
   submissions, and prove repeated process restart/fencing recovery. Until
   those steps are integrated, active lifecycle authority remains OPEN.
2. The ClickHouse cutover has **not been run against the user's actual
   production ClickHouse database**. The operator must quiesce all writers,
   produce and verify a restorable backup, review every manifest scope mapping,
   run the explicit cutover, then verify active and retained backup tables and
   application readers. No live database was modified by this code change.
3. The WebAuthn RP code has **not yet been bound to a trusted desktop identity
   provider or exercised with an enrolled physical authenticator**. A real
   credential registration and assertion must be completed in the configured
   production origin/RP environment; callback-based approval tests are not
   hardware credential acceptance.
4. No authenticated desktop approval UI or MFA/session API has been enabled.
   The existing desktop Operator Console remains read-only; do not add an
   approval route to it until identity, CSRF/origin protection, session
   revocation, RBAC and audit integration are complete.
5. Protective POST-to-ID and triggered child-fill logic still needs the
   authenticated Binance Demo/Testnet write-side drill, including response
   loss after POST, client-ID lookup, triggered algo-to-child association,
   cumulative partial fills, fee/funding reconciliation, and process restart.
6. Three-store acceptance remains an opt-in real ClickHouse + SQLite + venue
   drill. The current read-only CI fixture is not a substitute for repeated
   restart and injected-failure acceptance against the authorized testnet and
   persistent stores.

## Release decision

All seven requested gates remain OPEN until their own acceptance evidence is
attached. Never treat successful static tests or a passing simulated venue
fixture as proof of a live exchange, hardware WebAuthn, production database
cutover, or desktop identity integration. Keep Mainnet fail-closed.
