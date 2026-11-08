# AITOS storage backend and failover policy

## Design invariant

ClickHouse remains the analytical system of record for market history, feature
queries, statistical research and the existing ClickHouse query surface.
PostgreSQL is a constrained operational/paper fallback; it is not a drop-in
replacement for ClickHouse SQL or MergeTree analytical workloads.

Redis remains an ephemeral transport/cache. Neo4j remains optional graph
storage. Neither is promoted to durable trade-ledger storage.

## Runtime configuration

The configuration model accepts:

- `AITOS_STORAGE_BACKEND=auto|clickhouse|postgres`
- `AITOS_STORAGE_PRIMARY=clickhouse`
- `AITOS_STORAGE_FALLBACK=postgres`
- `AITOS_STORAGE_ALLOW_FALLBACK=true|false`
- `AITOS_STORAGE_REQUIRE_CLICKHOUSE_ANALYTICS=true|false`
- `AITOS_STORAGE_POSTGRES_URL=...`

Default behavior is ClickHouse-first with analytical requirements enabled.
In `auto` mode, the runtime first initializes ClickHouse market storage. If
that fails, PostgreSQL market-record fallback is eligible only when explicitly
allowed, a PostgreSQL DSN is configured, and
`AITOS_STORAGE_REQUIRE_CLICKHOUSE_ANALYTICS=false`. That mode logs that
ClickHouse analytical SQL is unavailable; it must not be treated as analytics
parity. If ClickHouse market storage is healthy but its operational journal
fails, the runtime may select PostgreSQL for the journal when fallback is
allowed and the PostgreSQL adapter advertises the complete required journal
capability set.

Explicit `clickhouse` mode disables fallback. Explicit `postgres` mode
selects PostgreSQL market-record and operational-journal adapters, but is
refused while `AITOS_STORAGE_REQUIRE_CLICKHOUSE_ANALYTICS=true`. This is a
constrained paper-runtime mode, not a general-purpose replacement for
ClickHouse.

## PostgreSQL schema lifecycle

Both PostgreSQL adapters use the shared versioned migration runner in
`aitos/storage/postgres_migrations.py`. Migration DDL and its version/checksum
record are committed in one transaction under a PostgreSQL advisory lock.
Concurrent process startup is serialized; a crash rolls back the in-flight
migration rather than leaving a false applied-version marker. Existing
unversioned adapter tables are adopted by the idempotent initial migration and
validated for required columns. A checksum or schema mismatch fails startup
closed. Future schema changes must add a new numbered migration; do not edit an
already-applied migration.

## PostgreSQL adapter guarantees and limits

The market adapter persists canonical market events, live analytics events,
OHLCV, order-book/depth events, trade ticks, funding, open interest and learning
experience records as JSONB records. It supports recent OHLCV reads and
idempotent event keys. It explicitly rejects arbitrary ClickHouse SQL queries.
The capability declarations describe these write/read operations; they do not
claim feature-store query or historical analytical parity.

The operational journal adapter persists trade snapshots, journal entries,
decision records, trade links, management decisions and outcome attribution
in an append-only table with unique idempotency keys. Duplicate retries are
ignored. These writes are not transactionally atomic with ClickHouse, Redis,
exchange execution, or the other adapter. There is no dual-write or silent
mirroring.

## Fail-closed rules

- If ClickHouse market/analytics storage is unavailable, startup stops unless
  the constrained PostgreSQL paper fallback is explicitly allowed and
  ClickHouse analytics are explicitly marked non-required.
- A healthy PostgreSQL connection is insufficient by itself: a backend may
  only be selected for capabilities its adapter actually implements.
- Unsupported analytical queries must raise an explicit capability error;
  they must never return an empty result as a substitute.
- No silent dropped write, automatic data migration, or automatic backend
  promotion is allowed.
- Switching back to ClickHouse after PostgreSQL fallback requires an explicit
  reconciliation/migration procedure. Automatic switchback and dual-write are
  not implemented.

## Free hosting acceptance boundary

The Render Free smoke service has verified internal PostgreSQL connectivity
and a raw idempotent write/read round trip. It does not yet execute the AITOS
repository adapters or a paper-trading lifecycle. Render Free PostgreSQL is
temporary and must not be treated as production durability. Render Key Value
without persistence is cache/transport only. Do not expose the database with
a world-open IP allowlist. Railway is only a fallback test platform after the
account's free allowance is verified; do not create usage-metered resources
when cost is unclear.

## Read-only reconciliation

`scripts/reconcile_postgres_journal.py` compares PostgreSQL operational
snapshots, journal entries and decision lifecycle records against the matching
ClickHouse `trades`, `journal_entries` and `decision_journal` tables. It
checks both directions, preserves duplicate counts, hashes canonical payloads,
and emits a JSON report. It defaults to a bounded seven-day window; pass
`--since` to select a different timezone-aware lower bound. Configure
`AITOS_RECONCILE_POSTGRES_URL` (or `AITOS_STORAGE_POSTGRES_URL`) and the
standard `CLICKHOUSE_*` variables before running it.

The command is deliberately read-only. It does not repair, copy, delete, or
promote data. A clean operational comparison is necessary evidence, not
sufficient authorization to switch backends. Market OHLCV, depth, ticks,
funding, learning experiences and analytical feature tables are outside this
first reconciliation scope and must not be inferred as reconciled.

## Not yet accepted

- Additional forward migrations for future schema evolution; the initial versioned migration and compatibility checks are implemented.
- Full AITOS paper-runtime acceptance on Render. The deployed smoke service
  now exercises the actual market-data and operational-journal adapters,
  schema migration v1, write/read, and repeat-request idempotency against the
  Render-hosted PostgreSQL database. GitHub Actions also tests ephemeral
  PostgreSQL 16. This is not a full scanner-to-trade paper lifecycle test.
- Full crash-window reconciliation across exchange execution state, operational
  journal and market records; the current CLI only compares operational
  journal/trade snapshots between ClickHouse and PostgreSQL.
- ClickHouse-to-PostgreSQL and PostgreSQL-to-ClickHouse reconciliation with
  counts, key coverage, checksums and explicit operator approval.
- Empirical paper-canary acceptance using real market-data flow and complete
  AITOS decision/trade lifecycle.

Until these are accepted, PostgreSQL is a constrained fallback/test adapter,
not a fully validated platform-level replacement for ClickHouse.
