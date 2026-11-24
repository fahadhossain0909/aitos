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
