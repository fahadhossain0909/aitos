# AITOS storage backend and failover policy

## Design invariant

ClickHouse remains the system of record for market history, order-book/depth
events, analytics, learning experiences, feature queries and statistical
research. PostgreSQL is an optional transactional fallback for the operational
trade/journal record adapter only. It is not a drop-in replacement for
ClickHouse query semantics or MergeTree-backed analytical workloads.

Redis remains an ephemeral transport/cache. Neo4j remains optional graph
storage. Neither is promoted to durable trade-ledger storage by this change.

## Runtime configuration

The configuration model accepts:

- `AITOS_STORAGE_BACKEND=auto|clickhouse|postgres`
- `AITOS_STORAGE_PRIMARY=clickhouse`
- `AITOS_STORAGE_FALLBACK=postgres`
- `AITOS_STORAGE_ALLOW_FALLBACK=true|false`
- `AITOS_STORAGE_POSTGRES_URL=...`

The default is ClickHouse-first. In `auto` mode the runtime initializes
ClickHouse market-data storage first, then tries the ClickHouse journal. If
only the journal adapter fails and PostgreSQL is configured and allowed, the
runtime uses the PostgreSQL operational journal adapter. Explicit
`clickhouse` mode disables fallback. Explicit `postgres` mode selects the
PostgreSQL journal adapter, but still requires ClickHouse for market and
analytics repositories.

## PostgreSQL adapter guarantees

The PostgreSQL journal adapter uses a connection pool and a transactional
append-only table with a unique idempotency key. It supports trade snapshots,
journal entries, and ordered retrieval of journal entries by trade. Duplicate
retries are ignored by the unique key. It does not claim multi-record atomicity
with ClickHouse; there is no dual-write or silent mirroring.

## Fail-closed rules

- If ClickHouse market/analytics storage is unavailable, paper/live startup
  stops. PostgreSQL journal fallback cannot satisfy market-data/analytics
  capabilities.
- If ClickHouse journal is unavailable and PostgreSQL is not configured,
  disabled, or unhealthy, startup stops.
- A healthy PostgreSQL connection is insufficient by itself: a backend may
  only be selected for capabilities its adapter actually implements.
- No empty-query fallback, dropped write, automatic data migration, or
  automatic backend promotion is allowed.
- Switching back to ClickHouse after a PostgreSQL fallback requires a separate
  reconciliation/migration procedure; this change does not auto-switch or
  dual-write.

## Free hosting acceptance boundary

Render Free PostgreSQL is temporary and must not be treated as production
durability. Render Key Value without persistence is cache/transport only.
Render's hosted SQL query connector cannot reach the database while its IP
allowlist is empty. Do not expose the database with a world-open allowlist.
An internal Render service with a private database connection is required for
a real smoke test. Railway is only a fallback test platform after the account's
free allowance is verified; do not create usage-metered resources when cost is
unclear.

## Not yet accepted

The PostgreSQL adapter currently covers operational journal records, not the
full ClickHouse repository surface. Full PostgreSQL market-event ingestion,
decision-journal parity, decision/outcome ledger parity, schema migration,
crash/restart reconciliation, and Render/Railway paper-canary acceptance remain
separate requirements before claiming a full platform-level fallback.
