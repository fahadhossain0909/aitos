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

## Durable paper execution intent and recovery

The ClickHouse-backed `order_execution_ledger` records a stable
`client_order_id` and the complete order request before the executor performs
the venue side effect. A terminal acknowledgement/fill is appended afterward.
If the process loses the response or crashes between submit and acknowledgement
persistence, restart queries the venue by client order ID. It never blindly
resubmits an intent whose outcome is ambiguous. Executors that cannot perform
an authoritative client-ID lookup leave the intent unresolved and require
operator reconciliation.

Paper startup also checks for acknowledged entry fills without a matching
open or closed trade snapshot. Such an unlinked fill blocks new entries rather
than silently allowing portfolio state to diverge. This is a fail-closed
recovery guard, not automatic position reconstruction. The current paper
executor is simulated; an exchange-backed executor must implement
`get_order_by_client_id` against the venue's authoritative order query before
this recovery path can be accepted for real exchange execution.

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

The Render Free smoke service now exercises the actual AITOS PostgreSQL
market-data and operational-journal adapters, versioned schema migration,
write/read, and repeated idempotent requests. This is adapter acceptance only,
not a full scanner-to-trade paper lifecycle. Render Free PostgreSQL is
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
`--since` and `--until` to select an explicit timezone-aware half-open interval (`since <= record_time < until`). Configure
`AITOS_RECONCILE_POSTGRES_URL` (or `AITOS_STORAGE_POSTGRES_URL`) and the
standard `CLICKHOUSE_*` variables before running it.

The command also compares every market-record type implemented by the
PostgreSQL fallback adapter: OHLCV, canonical market events, live analytics,
order-book snapshots, depth events, trade ticks, funding, open interest and
learning experiences. It normalizes database timestamp precision and
schema-version representations, compares both directions, and treats
duplicates as anomalies.

The command is deliberately read-only. It does not repair, copy, delete, or
promote data. A clean comparison is necessary evidence, not sufficient
authorization to switch backends. ClickHouse-only analytical/feature tables,
trade runtime state, portfolio drawdown state and exchange execution
acknowledgements are outside this reconciliation scope and must not be
inferred as reconciled. Use `--since` and `--until` to bound the report to the actual
fallback/outage interval rather than comparing periods when only one backend
was authoritative.

## Not yet accepted

- Additional forward migrations for future schema evolution; the initial versioned migration and compatibility checks are implemented.
- Full AITOS paper-runtime acceptance on Render. The deployed smoke service
  now exercises the actual market-data and operational-journal adapters,
  schema migration v1, write/read, and repeat-request idempotency against the
  Render-hosted PostgreSQL database. GitHub Actions also tests ephemeral
  PostgreSQL 16. This is not a full scanner-to-trade paper lifecycle test.
- Full cross-system crash-window reconciliation across exchange execution acknowledgements,
  position/runtime state, operational journal and market records. Durable order
  intent/acknowledgement journaling and fail-closed startup detection are now
  implemented, but automatic position reconstruction for an unlinked fill and
  exchange-specific client-ID lookup still require an exchange-backed executor.
- A controlled, operator-approved data-copy/switchback procedure. The current
  tool is a read-only comparison with counts, directional missing-key
  fingerprints, canonical payload checksums and duplicate detection; it does
  not perform data migration or backend promotion.
- Empirical paper-canary acceptance using real market-data flow and complete
  AITOS decision/trade lifecycle.

Until these are accepted, PostgreSQL is a constrained fallback/test adapter,
not a fully validated platform-level replacement for ClickHouse.
