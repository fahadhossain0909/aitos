# Persistence revision/fencing gate — evidence and approval proposal

Date: 2026-11-06 (UTC session date). Branch: `arena/ed127d35-aitos`.
Baseline HEAD: `6f8052b`; existing broad working changes preserved.

## Follow-up status

The founder subsequently approved the isolated prototype, now implemented and
verified without runtime wiring. See `REVISION_JOURNAL_PROTOTYPE_2026-11-06.md`.
The original proposal/assessment below is retained as its approval baseline.
No migration, active paper wiring or production activation has been authorized.

## 1. Executive summary / operating mode

**REPRODUCED:** the existing timestamp-versioned ClickHouse schema cannot provide
strict monotonic state revisions or preserve conflict evidence after replacement
merges. Eight new isolated counterexample tests exercise real embedded ClickHouse
and the current `DurableTradingStateStore` readers. No production implementation,
configuration, database, migration, trading state or service was changed.

These are passing **hazard-characterization** tests, not passing safety gates.
They deliberately demonstrate current limitations. When a replacement protocol is
implemented, they must be retained against the legacy path or replaced with
explicit approved safety-invariant tests—not silently treated as certification.

**Proposed next implementation, awaiting explicit approval:** an isolated,
not-runtime-wired single-host paper revision-journal prototype using SQLite
transactions (already a repository dependency/pattern), with CAS, scoped identity,
fenced writer generations, immutable revision events and a transactional outbox.
No new database server, live execution integration or migration is proposed for
that first prototype. Real cutover is a different gate.

## 2. Observed call path and module matrix

`TradeLifecycle` updates `trade.updated_at` using `utc_now_iso()`. Snapshot events
reach `TradeStatePersistence._save_event`; critical execution writes can also use
`persist_execution_state`. `DurableTradingStateStore.save_trade` serializes the
snapshot and inserts its wall-clock timestamp into `DateTime64(3)`.

Runtime and paper closed-ledger tables are `ReplacingMergeTree(updated_at)` with
`ORDER BY trade_id`. Recovery selects the maximum retained timestamp and checks
payload uniqueness at that timestamp. Closed IDs exclude runtime rows; closed
archive precedes runtime deletion. These safeguards remain unchanged.

| Component | Expected / observed | Status |
|---|---|---|
| Latest-retained conflict guard | Rejects conflicting tied payloads before merge | VERIFIED |
| Replacement merge | Collapses conflicting tied payloads into one retained row | REPRODUCED |
| Post-merge conflict guard | Accepts the survivor; cannot discover erased alternative | REPRODUCED limitation |
| Runtime ordering | Selects greatest wall-clock value, not logical event revision | VERIFIED / counterexamples REPRODUCED |
| Account/mode identity | No scoped key or enforced scope predicate in these tables/readers | VERIFIED; collision counterexamples REPRODUCED |
| Income cursor | Timestamp-selected cursor can decrease | REPRODUCED |
| Venue, Redis, market feed, application recovery | Not started or exercised in this increment | NOT TESTED |
| Deployed ClickHouse transport/version | No external server used | NOT TESTED |

## 3. Executed counterexamples and root causes

Test source: `tests/test_clickhouse_revision_hazards.py`.

| Scenario | Actual current result | Meaning / severity |
|---|---|---|
| Runtime tied payloads, before/after forced merge | Reader initially blocks; physical rows change 2 -> 1; reader then accepts survivor | P1: uniqueness over retained rows cannot certify original history |
| Closed ledger tied payloads, before/after forced merge | History initially rejects; after merge accepts one PnL from {1, 7} | P1: no authoritative tied winner is established |
| Later logical reduction after clock rollback | Insert quantity 1 at `.002`, then 0.5 at `.001`; restores quantity 1 | P1: wall-clock order is not business revision order |
| Simulated stale writer with ahead clock | Current quantity 0.5 is superseded by stale quantity 1 | P1: no storage-side writer generation/CAS enforcement |
| Two distinct sub-millisecond updates | `.001100` and `.001900` become one DateTime64(3) version; reader blocks | P1 availability/integrity gate: increasing precision alone would not solve ordering/fencing |
| Reused ID across two synthetic accounts | Only the later account's symbol is restored | P1 conditional scope defect: relevant if identifiers are reused/shared; not evidence of real UUID collision |
| Closed ID in one scope, open in another | Unscoped closed-ID exclusion suppresses the open row | P1 conditional scope defect; not evidence that live/paper currently share reused IDs |
| Cursor 200 followed by 100 at newer timestamp | Loader returns 100 | P2: latest timestamp is not a monotonic high-water mark; duplicate/replay cost needs downstream review |

**ROOT CAUSE CONFIRMED:** the storage key/version and readers provide timestamp
replacement semantics, not compare-and-swap, monotonic sequencing or fenced
ownership. Replacement can erase precisely the competing evidence on which the
newer fail-closed reader depends. More timestamps, SELECT FINAL, in-process locks
or a new integer column alone cannot establish those missing guarantees.

No actual account loss, double order, real collision, hostile writer or production
incident is claimed. The stale-writer and cross-scope cases are controlled
synthetic conditions, not an observed deployed topology. Existing closed archive
suppression still prevents its intended same-identity crash-window resurrection;
it does not establish cross-account isolation.

## 4. Proposed contract — NOT IMPLEMENTED

### Identity and authority

- Identity is a trusted, explicit tuple `(mode, venue, account_scope, run_scope,
  trade_id)`. Scope metadata comes from approved runtime configuration, not a
  browser-supplied account field. Backtest/simulation runs need separate identity.
- A persistent transactional authority assigns revisions; caller wall clocks
  remain diagnostic metadata only. Missing/unknown scope blocks import/startup.
- For the first prototype only: use local SQLite with WAL and FULL synchronous
  transactions, following existing intent-store patterns. Do not modify existing
  execution/switch journals or reuse their live files in tests.
- One schema/module should own the prototype protocol, not a competing config or
  telemetry framework. SQLite is only for one host/local durable disk. It does
  not certify multi-host/network-filesystem operation or replace the need for a
  separately approved distributed transactional authority later.

### Atomic write protocol

One `BEGIN IMMEDIATE` transaction must:

1. Check the active writer identity/generation for the trusted scope.
2. Check the idempotency key. Same key + same versioned canonical business payload
   returns the original committed result; changed payload is a conflict. Duplicate
   acknowledgement does not authorize replacing the caller's current state with
   an older acknowledged snapshot. A stale generation is rejected even on retry.
3. Compare the caller's expected revision with the current head (zero only for
   an absent identity). Reject stale expectations; never silently last-write-win.
4. Append the immutable revision event, update the head and add its outbox record
   in the **same database transaction**. Revisions start at one and increase by one.
5. Commit before acknowledgement. A timeout is ambiguous until the idempotency key
   is queried/retried; it is not proof of failure or permission for a venue retry.

Integers must reject booleans and be bounded to signed 64-bit positive ranges
compatible with SQLite; detect exhaustion before modifying state. Schema version,
strict finite JSON and deterministic serialization/digest rules must be explicit.
A digest detects conflicting retries, not tampering by a host that controls the DB.

Writer generation is advanced transactionally during an explicitly authorized
ownership change; old writers cannot append after it changes. No wall-clock lease
or automatic remote takeover is implied. This **fences storage only**: it does not
fence an old process from submitting an exchange order, nor cancel in-flight
requests. Live order ownership requires a separate execution-level review/gate.

### Terminal state, recovery and projection

- A terminal event is retained, never an asynchronous DELETE as the source of
  truth. The same trade identity must not reopen. A new position needs a new ID.
- The initial prototype must reject differing terminal snapshots. Later fee/
  funding/PnL corrections require a separately specified amendment event with
  original-event linkage; they must not silently overwrite the close record.
- Recovery reads the committed scoped head and validates its revision/event
  references. Inconsistency blocks certification; no fallback to an arbitrary
  surviving ClickHouse row. Monitoring-preservation behavior before any real
  cutover still requires a dedicated integration design/test.
- The outbox is committed with state, then projected at least once into a separate
  append-only ClickHouse `MergeTree` evidence table. Retain `(scope, trade_id,
  revision, event_id, payload_digest)`; no `ReplacingMergeTree` conflict erasure.
- A projector crash after sink insert but before local acknowledgement may create
  duplicate sink rows. Verify identical duplicates; reject mismatched event or
  revision digests. Do not advertise cross-database exactly-once transactions.
- In the prototype the projection is test-only, not a changed console reader or
  active persistence path. Projection freshness/capacity gates precede adoption.

### Alternatives considered

| Alternative | Decision / reason |
|---|---|
| DateTime64(6/9), MAX timestamp or FINAL | Rejected as a correctness solution: no causal CAS/fence; replacement can still destroy evidence |
| Only a per-process counter/lock | Rejected: resets on restart and cannot arbitrate independent processes |
| Only ClickHouse UInt64 revision + ReplacingMergeTree | Insufficient: revision assignment/conflict checks are not atomic authoritative CAS |
| Existing Redis as the only new sequencer | Deferred: durability/failover and atomic coordination with state/outbox are not established; no Redis settings will change |
| New distributed transactional server | Deferred: larger dependency/operations change; needs explicit topology choice and approval |
| Local transactional journal + outbox | Proposed for an isolated single-host paper prototype only; reuses an existing dependency/pattern without claiming distributed safety |

## 5. Migration, rollout and rollback gates — NOT EXECUTED

1. Approve the isolated prototype contract. Implement only temporary-database
   tests and a disabled/unwired module; no runner, event schema or config change.
2. Verify CAS races, duplicate/conflicting retries, generation takeover, stale
   writer rejection, restart/reopen, cross-scope separation, terminal protection,
   bounds, transaction rollback and outbox crash windows using separate processes
   where ownership/concurrency matters.
3. Separately review ownership, execution-intent coordination, reader contracts,
   failure/quarantine behavior, latency budgets and data-protection requirements.
4. Produce a non-destructive legacy inventory/import plan. A retained row cannot
   reconstruct merged-away conflicts. Never infer complete lineage from timestamps
   or automatically label legacy data verified. Ambiguous rows need operator
   reconciliation/provenance; do not invent events or silently discard rows.
5. Only after specific approval: isolated paper wiring and a real-service restart/
   reconciliation/soak rehearsal. Compare projections before making one authority
   canonical. Trading behavior/risk policy changes remain out of scope.
6. Production migration/cutover/live activation are separate approvals and are not
   authorized or performed by this proposal.

Current rollback: none needed for runtime; this increment adds tests/docs/CI only.
Stopping/removing the isolated tests leaves deployed state untouched. A future
post-cutover rollback is **not** simply toggling back to stale legacy readers:
reconcile authoritative heads and projections under an approved operational plan.
Never drop tables, delete intents/ledgers or reset risk controls as rollback.

## 6. Verification and limitations

Final executed commands:

```sh
.venv/bin/pytest tests/test_clickhouse_embedded_ledger.py \
  tests/test_clickhouse_revision_hazards.py -m integration --timeout=90 \
  --durations=5 --junitxml=tmp/revision-gate/embedded.xml -q
.venv/bin/pytest -q -m 'not integration' --timeout=30
.venv/bin/black --check tests/test_clickhouse_revision_hazards.py
.venv/bin/flake8 --select=F tests/test_clickhouse_revision_hazards.py
.venv/bin/python -m compileall -q tests/test_clickhouse_revision_hazards.py
git diff --check
```

| Evidence | Observed |
|---|---|
| Existing SQL safeguards + new hazard characterization | **14 passed in 1.54s** |
| Backend nonintegration regression | **1783 passed, 30 deselected in 55.71s** |
| Focused Black / F-lint / compile / whitespace | VERIFIED successful |
| Environment | Python 3.11.2, chdb 4.4.0, embedded ClickHouse 26.9.2.1 |
| Artifacts | ignored `tmp/revision-gate/embedded.log`, `embedded.xml`, `tmp/backend-revision-gate.log` |
| PR/manual CI definition | `.github/workflows/persistence-quality.yml` added; remote execution NOT TESTED |
| Frontend/gateway browser tests | Unchanged and NOT TESTED again in this increment; prior results remain historical |
| Restart/power loss/disk failure/multi-host | NOT TESTED; prototype not implemented |
| Production stage p50/p95/p99/capacity | NOT MEASURED; test elapsed times are not latency evidence |
| Market data/event bus/candidates/orders/account reconciliation | NOT TESTED; no runtime or exchange connection started |
| Independent review | NOT TESTED; no separate reviewer/subagent was available |

The previous 22 deselected integration cases increased to 30 because these eight
new counterexamples are explicitly integration-marked. Passing counts do not
change the blocked/un-certified release posture. No frontend, production module,
strategy, risk limit, Redis setting, deployment or non-test database was changed; no commit
or push was made.

## Approval requested

Approve building the **unwired single-host paper revision-journal prototype and
isolated tests described above**, using existing SQLite support, with CAS/scoped
identity/fenced generations/terminal preservation/transactional outbox. This is
not approval for migrations, runner activation, real account access or deployment.
