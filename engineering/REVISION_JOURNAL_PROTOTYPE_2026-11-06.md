# Unwired paper revision-journal prototype — 2026-11-06

## Outcome and authorization

The founder explicitly approved the isolated prototype proposed in
`PERSISTENCE_REVISION_GATE_2026-11-06.md`. **VERIFIED:** the module and temporary-
database tests now implement that bounded prototype. It is **not imported by any
application runner**, not a replacement for current recovery, and not deployed.
No existing execution/switch journal, production database, event schema, runner,
Redis configuration, strategy, risk limit or live order path was changed.

Branch: `arena/ed127d35-aitos`; baseline HEAD `6f8052b`; the broad existing working
tree was preserved. No commit/push, migration, destructive cleanup or real account
access. All new database writes and forced merges were in pytest temporary files.

## Implemented contract

`aitos/trading/revision_journal.py` uses only Python standard-library dependencies.
Construction requires an explicit standalone filename in an existing directory.
There is no default path, CLI, config flag, task loop, automatic takeover or app
registration. Existing files with unrelated tables or unsupported metadata are
rejected rather than repurposed. Importing the module does not open a database.

| Component | Implemented behavior | Evidence |
|---|---|---|
| Scope | Paper/backtest only; venue/account/run identity plus trade ID; bounded ASCII identifiers | Scope separation and live/unknown rejection tests |
| SQLite authority | Local file, WAL, FULL synchronous connections, foreign keys, BEGIN IMMEDIATE writes | Reopen/settings/rollback tests |
| Ownership | CAS on expected generation, then increment persisted generation | Separate-process ownership race has one winner |
| Storage fencing | Every append/ack verifies current writer ID and generation first | Old generation rejected, including duplicate retry and ACK |
| Revisions | Per-scoped-trade expected-head CAS; revisions start at 1 and increase | Separate-process same-head append race has one winner |
| Idempotency | Scoped event ID; identical canonical business payload returns original event plus current head revision | Retry after newer head does not overwrite state |
| Terminal record | Close stays in immutable history; a new event cannot reopen or amend it | Terminal/retry/reopen tests |
| Atomic commit | Event, head and outbox are one transaction; no source-of-truth DELETE | Trigger-abort and process-death tests |
| Recovery | Checks full trade revision sequence, canonical payload/digest, terminal history, head and outbox references | Old/history/head/outbox corruption tests fail closed |
| Outbox | Bounded pending batch, explicit fenced ACK; delivered rows retained | Lost-ACK/redelivery and forged-receipt tests |
| Test projection | Declared append-only MergeTree schema; bounded strict duplicate/conflict verifier | Real embedded insert/merge retains both duplicates/conflicts |

Generations/revisions use validated signed-64-bit integers, rejecting booleans and
out-of-range inputs; generation exhaustion is explicitly rejected. JSON is a
version-1 deterministic Python JSON representation (sorted keys, compact UTF-8,
finite values), not a claim of RFC 8785 interoperability. Payloads are limited to
64 KiB, 10,000 structural nodes and depth 16. Unknown types, nonstring keys,
nonfinite numbers and invalid UTF-8 are rejected. SHA-256 checks canonical payload
consistency; they are not a tamper-proof audit against someone controlling the DB.

`Revision` is immutable API evidence and returns a fresh decoded payload copy.
The API has no event-update/delete method. This does not prevent a privileged
local process from editing SQLite directly. A duplicate ACK is not a command to
replace current in-memory state with an older event.

## Process and crash evidence

`tests/test_revision_journal.py` uses spawn-created independent processes, SQLite
connections and explicit barriers:

1. Two processes append against revision zero: one commits, one gets CAS conflict;
   there is exactly one head/event/outbox result.
2. Two processes request ownership from generation one: one acquires generation
   two, one conflicts. The previous writer is rejected; the new writer can append.
3. A child exits abruptly **inside the transaction**, during the outbox insertion
   trigger after event/head writes. On reopening, no partial event/head/outbox
   survives. The exit function/trigger exists only in temporary test DBs.
4. A child commits then exits before the application can observe acknowledgement.
   On reopen the event and pending outbox survive; same-event retry is a duplicate.

These are actual process-exit tests, **not** power-cut, hardware-cache, storage-
controller, disk-full, filesystem corruption or host-failover certification.
WAL/FULL settings alone are not evidence for those unexecuted failure modes.

## Projection boundary

`PROJECTION_DDL` is a declaration, never executed by the journal or a runner.
`tests/test_revision_journal_projection.py` executes it only in an embedded test
engine. It uses plain MergeTree, not ReplacingMergeTree: exact retries and
conflicting records both survive FINAL merges.

`verify_projection_batch` validates up to 1,000 records, accepts only exact full-
envelope duplicates, and rejects disagreeing scoped event IDs or revision keys.
It validates a **batch**, not all sink history or cross-batch uniqueness. An
actual projector must fetch/verify relevant existing sink evidence before ACK;
no active projector or automatic network sink is included. The test models a
sink insert followed by lost local ACK, then redelivery and explicit ACK.
Acknowledgement is a trusted caller operation, not an independently attested
remote receipt. No cross-database exactly-once guarantee is claimed.

## Final executed verification

| Check | Observed result |
|---|---|
| Focused journal + projection | **39 passed in 5.69s** (37 unit/process + 2 embedded projection) |
| Full nonintegration backend | **1820 passed, 32 deselected in 62.91s** |
| Separate embedded safeguard/hazard/projection suites | **16 passed in 1.66s** |
| Black/F-lint/Bandit | VERIFIED successful on new module/tests as applicable |
| Python compilation / whitespace | VERIFIED successful |
| Workflow YAML and permissions | VERIFIED local parse, read-only repository permission |
| Application import check | VERIFIED AST scan found no application import of prototype |
| GitHub workflow execution | NOT TESTED remotely |
| Frontend/gateway browsers | Unchanged; NOT TESTED again in this increment |
| Independent review | NOT TESTED; no independent reviewer/subagent was available |

Counts overlap: the 37 new unit/process tests are included in 1820; the two new
projection tests are included in both focused 39 and embedded 16. The eight legacy
hazard cases still pass by reproducing **unresolved production limitations**.
They do not mean the old production persistence has been fixed.

Commands:

```sh
.venv/bin/pytest tests/test_revision_journal.py \
  tests/test_revision_journal_projection.py --timeout=90 -q
.venv/bin/pytest -q -m 'not integration' --timeout=60
.venv/bin/pytest tests/test_clickhouse_embedded_ledger.py \
  tests/test_clickhouse_revision_hazards.py \
  tests/test_revision_journal_projection.py -m integration --timeout=90 -q
.venv/bin/black --check aitos/trading/revision_journal.py tests/test_revision_journal*.py
.venv/bin/flake8 --select=F aitos/trading/revision_journal.py tests/test_revision_journal*.py
.venv/bin/bandit -q aitos/trading/revision_journal.py
git diff --check
```

Ignored logs: `tmp/revision-prototype/focused.log`, `backend.log`, `embedded.log`.
Environment remains Python 3.11.2 / chdb 4.4.0 / embedded ClickHouse 26.9.2.1.
The no-migration persistence workflow now includes prototype and projection tests.

## Deliberate limitations / remaining gates

- **Storage fence only:** a stale process might still submit a venue order unless
  separately fenced at the execution layer. This prototype cannot submit any.
- Single-host local durable disk only. No distributed authority, shared-network
  filesystem, multi-host failover, clock lease or automatic leader election.
- Payload validation is strict structural JSON plus matching trade identity and
  recognized state, **not** complete production Trade/financial reconciliation.
- No legacy importer, migration, runner recovery wiring, account reconciliation,
  console integration, live scope, order API or financial amendment protocol.
- The prototype rejects all new terminal snapshots after close. Later fees/funding
  adjustments require an explicit reviewed amendment protocol, not overwriting.
- History validation is streaming O(history); pending validation scans the scope's
  histories. No p50/p95/p99/capacity/soak measurement or production latency claim.
  These integrity checks must not be removed merely to make a benchmark faster.
- No state cache synchronization protocol, deployment security review, independent
  audit, secure backup/restore or external ClickHouse transport validation.
- Market-data/event-bus/candidate/venue runtime health and production account state
  were NOT TESTED. Test elapsed times are not trading-path latency measurements.

Next gate: independent contract/diff review and design of production Trade binding,
monitoring preservation, execution-intent ownership, financial amendments and
non-destructive legacy reconciliation. Only after explicit approval should a
separate isolated paper-runner wiring/restart/soak rehearsal begin. Neither that
wiring nor any production migration/cutover is authorized by this prototype approval.

## Rollback

There is no runtime cutover to roll back. Stop the temporary tests or cease using
the explicit prototype file; application behavior remains on its existing path.
Do not point this module at existing execution/switch journal files, delete ledgers,
change risk settings, reset intents or switch storage authority as a shortcut.
Any future authority switch requires its own reconciled rollback procedure.
