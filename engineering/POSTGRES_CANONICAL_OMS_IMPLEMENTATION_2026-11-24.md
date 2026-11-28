# PostgreSQL canonical OMS — implementation increment

The branch now includes a PostgreSQL-native OMS journal and schema migration.
It is deliberately separate from the existing SQLite paper/test journal.

## Guarantees implemented
- Scope-keyed writer generations acquired by compare-and-swap under a row lock.
- Append-only aggregate event revisions, serialized by a per-aggregate head lock.
- Idempotent event IDs; conflicting reuse is rejected.
- Canonical JSON payload hashing and verifiable per-aggregate SHA-256 chain.
- Transactional outbox rows committed with each OMS event.
- Outbox claims use `FOR UPDATE SKIP LOCKED`; projection acknowledgement is
  conditional on the exact event hash and is idempotency-safe.
- PostgreSQL migration version 2 is checksummed and applied under the existing
  migration advisory transaction lock.

## Still not a production cutover
The existing TradeLifecycle and ExecutionIntentStore call graph has not yet
been converted to use this PostgreSQL authority for every financial transition.
The OMS module must not be described as active lifecycle integration until
entry, fills, protection, partial reductions, exits, fees/funding, restart
hydration and account-level equity/risk state all commit through this store.
No database was migrated and no runtime reader/writer was switched.
