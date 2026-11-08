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

## Not yet accepted

- Versioned, ordered PostgreSQL schema migrations and compatibility checks.
- AITOS adapter round-trip, concurrent duplicate/retry and restart-recovery
  tests against the Render-hosted database (the GitHub Actions integration
  job uses an ephemeral PostgreSQL 16 service).
- Crash-window reconciliation across execution state, operational journal and
  market records.
- ClickHouse-to-PostgreSQL and PostgreSQL-to-ClickHouse reconciliation with
  counts, key coverage, checksums and explicit operator approval.
- Empirical paper-canary acceptance using real market-data flow and complete
  AITOS decision/trade lifecycle.

Until these are accepted, PostgreSQL is a constrained fallback/test adapter,
not a fully validated platform-level replacement for ClickHouse.
