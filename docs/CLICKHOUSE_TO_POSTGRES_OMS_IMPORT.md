# ClickHouse → PostgreSQL Canonical OMS Import Runbook

This procedure imports existing ClickHouse trade state into the PostgreSQL
canonical OMS. It is an offline migration, not a live dual-write mode. The
importer never renames, drops, or mutates ClickHouse tables.

## Preconditions

- Identify the exact ClickHouse database and the PostgreSQL primary database.
- Stop every process that can write to the source ClickHouse tables. Keep them
  stopped through import, parity verification and reader/writer routing.
- Create and verify a restorable ClickHouse backup. Retain the original tables
  and backup tables until the rollback window is formally closed.
- Use a PostgreSQL role that can apply the versioned OMS migrations and insert
  into canonical OMS/import-evidence tables.
- Prepare explicit account-scope attribution for legacy v1 rows. Do not infer a
  scope from a symbol, strategy, hostname or process name.
- Use a private directory for the JSONL manifest. The importer creates the
  manifest with mode 0600 and refuses to overwrite an existing manifest or
  partial manifest.

## Source selection

The importer detects the source schema through `system.columns`:

1. If the active table is already scope-keyed, it reads the active table and
   checks the retained pre-scope backup row count.
2. If the active table is legacy and a scope-keyed `*_v2` table exists, it
   reads the v2 table and checks its row count against the legacy source.
3. Otherwise it reads the legacy source and requires explicit scope attribution
   from the source payload or `--scope-map`.

For scope-keyed rows it re-computes the original source fingerprint and the
canonical payload SHA-256. Any mismatch aborts the import. For legacy rows it
preserves exact JSON text and requires the account scope to be explicit.

## Dry run

Set ClickHouse connection variables in the private environment:

- `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`, `CLICKHOUSE_USER`,
  `CLICKHOUSE_PASSWORD`, `CLICKHOUSE_DATABASE`, `CLICKHOUSE_SECURE`
- `DATABASE_URL` is not required for dry-run mode.

For legacy rows without an embedded scope, create a JSON object mapping every
source fingerprint to an account scope, for example:

```json
{
  "64-character-source-fingerprint": "paper:account-a"
}
```

Run:

```sh
python scripts/import_clickhouse_state_to_postgres_oms.py \
  --scope-map /secure/path/legacy-scope-map.json \
  --manifest-out /secure/path/clickhouse-oms-import.jsonl
```

For already scope-keyed sources, `--scope-map` may be omitted. Inspect the
row counts, scope distribution, source table selection and manifest before
proceeding. The manifest contains raw legacy payload text and must be protected
as financial data.

## Apply

Only after the writer freeze and restorable backup have been independently
verified, set `DATABASE_URL` to the PostgreSQL primary and run:

```sh
python scripts/import_clickhouse_state_to_postgres_oms.py \
  --scope-map /secure/path/legacy-scope-map.json \
  --manifest-out /secure/path/clickhouse-oms-import.jsonl \
  --apply \
  --confirm-writers-stopped \
  --confirm-backup-retained
```

The importer applies PostgreSQL migrations (including the immutable v3 import
evidence ledger), stages source rows in a bounded local SQLite spool, and
imports by account scope and trade timestamp. It stores exact source JSON and
its SHA-256 in `aitos_oms_legacy_imports`. Canonical OMS events reference the
immutable evidence by source table, fingerprint and occurrence; lifecycle
hydration resolves that reference and verifies trade ID, timestamp and hash.

The importer is resumable and idempotent. A rerun after interruption reuses the
same deterministic event IDs and verifies existing evidence. It refuses
ambiguous same-timestamp conflicting snapshots and refuses to append legacy
history to an aggregate that already contains non-legacy canonical events.
Those conflicts require an explicit reconciliation plan; do not bypass them.

## Verification and cutover

Before routing the application to PostgreSQL:

- Compare the source row counts with the import manifest and PostgreSQL evidence
  ledger for each source table and account scope.
- Recompute per-record raw payload SHA-256 values from the manifest and compare
  them with `aitos_oms_legacy_imports`.
- Verify every manifest record has its deterministic canonical OMS event and
  that every event resolves to immutable source evidence.
- Start the lifecycle in recovery-only mode and verify open/closed Trade
  hydration for every scope. Investigate any missing, extra or unresolved
  aggregate.
- Verify the PostgreSQL event hash chain, writer generation/fencing and
  append-only triggers.
- Only then switch application readers/writers to PostgreSQL canonical OMS.
  Keep ClickHouse source and retained backup tables read-only throughout the
  rollback window.

The importer itself does not perform the routing switch or claim a production
cutover. A rollback must first stop writers, preserve PostgreSQL evidence and
event history, then restore the previous application routing. Never resume
legacy ClickHouse writes while PostgreSQL canonical writes are active.

## Current status

The migration path, versioned evidence schema, streaming importer, manifest,
idempotent event references, scope-keyed source support and integration tests
are implemented. No user production database has been imported, and no real
cutover or post-cutover restart acceptance has been performed.
