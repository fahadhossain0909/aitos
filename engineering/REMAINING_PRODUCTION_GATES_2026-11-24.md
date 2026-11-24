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

### Live approval and desktop identity boundary
- `aitos/security/live_approval.py` implements action-digest binding, server-side
  role checks, short-lived HMAC-signed approval receipts, one-time durable
  consumption and append-only audit. PostgreSQL OMS events and imported source
  evidence are append-only at the database level.
- `aitos/security/webauthn_rp.py` now supplies a py_webauthn RP with user-bound
  registration and assertion challenges, exact RP/origin binding, UV-required
  cryptographic verification, credential counters, one-time challenge
  consumption, credential inventory and append-only credential audit.
- `aitos/console_gateway.py` now binds sessions and pending MFA state to
  configured operator identities, requires WebAuthn for non-loopback login,
  supports guarded first-device enrollment, step-up enrollment of backup
  authenticators, session revocation and server-owned RBAC approval routes.
  Multi-operator password directories and role maps are server-side config.
- The desktop now contains the WebAuthn login ceremony, authenticator manager
  and Approval Desk. The desk signs an action digest only; it does not submit
  orders or execute trading mutations. The signed receipt consumer still needs
  a trusted service-to-service integration with the executor before it can
  authorize any production action. Physical hardware enrollment and external
  origin acceptance remain untested.

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
| WebAuthn / desktop approval | RP adapter, identity-bound MFA, backup-key management, RBAC and approval UI implemented | Hardware authenticator, external origin and trusted executor receipt-consumer acceptance outstanding |

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
  exposes canonical aggregate inventory/snapshot recovery, and hydrates the
  live lifecycle from verified PostgreSQL snapshots.
- `run_live_trading.py` now requires `DATABASE_URL`, validates/applies the
  PostgreSQL migrations, acquires a fresh account-scope writer fence, binds
  the canonical writer, and replaces projected open/closed lifecycle state
  with canonical snapshots before startup reconciliation. Legacy projected
  open trades without a matching canonical aggregate cause startup to fail
  closed pending controlled import.
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

1. The live entrypoint now requires `DATABASE_URL`, applies and verifies
   the PostgreSQL schema, acquires a new scope-specific fencing generation,
   binds the PostgreSQL-first lifecycle writer after the durable projection
   callback is configured, and hydrates lifecycle state from verified canonical
   snapshots before module initialization and account reconciliation. The
   hydration path refuses to start if a projected open trade has no canonical
   aggregate or if a canonical aggregate is in an unresolved intermediate
   state. This is runtime code integration, but the PostgreSQL production
   authority gate remains OPEN until current-head CI, deployment configuration,
   multi-process fencing, and repeated restart acceptance pass.
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
4. The authenticated desktop MFA and Approval Desk are now implemented.
   Approval receipts are not connected to a live order-execution route; the
   console remains read-only. A trusted server-side receipt consumer, physical
   authenticator acceptance and external HTTPS/RP acceptance remain open.
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


## Additional implementation — ClickHouse to PostgreSQL canonical import

- `scripts/import_clickhouse_state_to_postgres_oms.py` now streams both
  legacy or already scope-keyed ClickHouse source layouts into a bounded local
  SQLite staging spool. It selects the active v2/cutover table when present,
  checks companion retained-table counts, verifies source fingerprints and
  canonical payload hashes, and emits a JSONL manifest without loading the
  entire source dataset into memory.
- The PostgreSQL migration v3 creates an immutable import-evidence ledger.
  Each exact legacy JSON payload is stored as text plus SHA-256; canonical OMS
  events reference that immutable evidence by source table/fingerprint/
  occurrence, keeping event payloads bounded. Lifecycle hydration resolves the
  reference, verifies source trade ID, timestamp and raw SHA-256, then rebuilds
  the Trade snapshot with its explicit account scope.
- Apply is resumable and idempotent, requires writer-stop and restorable-backup
  acknowledgements, refuses ambiguous same-timestamp conflicting snapshots,
  refuses to append legacy history over an existing non-legacy canonical
  aggregate, and verifies every canonical source-event reference. It never
  mutates ClickHouse tables. No production data has been imported or cut over.
- Tests now cover read-only dry-run, legacy and scope-keyed sources, ambiguity
  rejection, PostgreSQL evidence immutability, and canonical Trade hydration.
  The actual database import, source/target per-record reconciliation and
  application reader/writer cutover remain operational acceptance gates.

## Additional implementation — WebAuthn login challenge lifecycle

- The relying party now issues short-lived, user-bound registration and
  authentication options, persists one-time challenges, checks credential/
  challenge ownership, enforces user verification and consumes challenges in
  the same transaction as authenticator counter updates.
- The desktop gateway binds pre-authentication and authenticated sessions to
  operator identity, supports multiple configured identities, requires WebAuthn
  MFA on non-loopback origins, supports out-of-band first-device bootstrap,
  backup-authenticator step-up enrollment, and role-gated approval challenge/
  assertion routes. Credential listing/revocation is scoped to an identity;
  revocation is intentionally offline-admin only and audited.
- The Approval Desk creates a signed action-digest receipt after a fresh
  WebAuthn assertion. It does not submit or execute orders. The executor's
  server-side receipt consumer still needs a secure service integration, and
  hardware/external-origin acceptance remains open.
