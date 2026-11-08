# Typed paper recovery and timestamp fix — 2026-11-06

## Follow-up

The next isolated increment now implements immutable fee/funding amendments; see
`FINANCIAL_AMENDMENTS_2026-11-06.md`. The original batch status and measurements
below are retained as historical evidence. Active production integration remains
unimplemented; the new contract is not venue-reconciliation certification.

## Executive summary / operating mode

Implemented the next safe remaining batch: full-schema Trade binding for the
isolated revision journal, consistent bounded recovery, a monitoring rehearsal,
read-only legacy assessment and a reproducible synthetic latency microbenchmark.
A separate reproducible production-source serialization defect was fixed.

**VERIFIED:** all new tests and current backend/frontend/browser regressions pass.
**Not all project release work is complete.** There is no production persistence
cutover, real account reconciliation, live activation, independent certification,
financial amendment protocol or native vault/installer. See the completion ledger
below; passing tests do not authorize those capabilities.

Branch `arena/ed127d35-aitos`, baseline `6f8052b`; broad existing work preserved.
No real order, deployment, migration, risk/strategy/Redis setting change, destructive
cleanup, commit or push. New journal/database writes used temporary synthetic files.
The existing sample preview was not connected to real financial data.

## Reproduced defect and smallest source fix

**AUD-20261106-50, P1 — ROOT CAUSE CONFIRMED:** `Trade.to_dict()` omitted the existing
`updated_at` field. Lifecycle events serialize through this method. The persistence
event handler reconstructs Trade through `_trade_from_dict`, whose missing-field
fallback is entry time. Consequently distinct updates could be written using the
same entry timestamp instead of their actual update times.

Before the fix, `test_trade_snapshot_keeps_actual_update_timestamp` failed with
`KeyError: 'updated_at'`. The ignored initial failing test output is recorded at
`tmp/revision-prototype/timestamp-reproduction.log`. After adding the one field to
`aitos/models/trade.py`, an actual `TradeStatePersistence._save_event` round-trip
regression verifies two distinct timestamp/quantity updates survive unchanged.

The field already exists in the model and decoder; serialization now preserves it.
This additive snapshot fix does not establish monotonic revisions, reconstruct old
rows or solve replacement-merge hazards. No service was restarted to activate a
new persistence architecture. Older payloads and retained history still require
separate assessment. No production incident or actual financial loss is claimed.

## Implemented remaining contracts

### Typed binding

`aitos/trading/paper_revision.py` adds `encode_trade`, `decode_trade` and a paper-
only `PaperRevisionStore` adapter. It imports no venue/client/Redis service and is
not registered by a runner. The original low-level journal remains unwired.

The codec requires the complete current Trade field set; unknown/missing fields
fail instead of being discarded or silently defaulted. It validates finite
numeric types (not booleans/strings), enums, explicit timezone timestamps, known
position states, price/exposure bounds, quantity/notional consistency, flags,
lists, partial exits and cost-adjustment structures. Recorded net PnL must agree
with gross minus fees plus funding, within the declared floating-point tolerance.
Closed records need fill/time/PnL evidence. It preserves unknown nullable values
rather than inventing measurements. Model normalization that would change the
recorded payload is detected and rejected.

This is full **schema/selected financial-invariant validation**, not a proof of
strategy suitability, complete venue fills, risk approval, market freshness or
account reconciliation. In particular it does not certify every possible derived
telemetry relationship or create a fee/funding amendment protocol.

### Recovery and monitoring rehearsal

`RevisionJournal.heads` reads one SQLite snapshot, validates all selected histories
and returns a complete bounded scope. Exceeding the limit throws; it never returns
truncated financial state. Orphaned head/event/outbox references block recovery.
`is_current` observes storage ownership at one point in time only.

Recovered revision records are immutable; decoded Trade objects are fresh copies.
All typed heads must validate before a financial recovery snapshot is certified.
An invalid snapshot is rejected; emergency monitoring of partially corrupt
portfolios remains a separate pre-cutover design requirement, not a solved claim.

`rehearse_monitoring` attempts every valid open position even when another callback
fails. Financial records remain available in the returned snapshot. It reports
missing feeds, failed registrations, unresolved-intent prerequisites and current
writer status. Callback mutation cannot change identity/feed checks or the saved
financial evidence. A takeover during registration invalidates readiness.

**Important:** `recovery_prerequisites_met` is a rehearsal observation.
`new_entries_allowed=False` and `live_authorized=False` always. This helper cannot
enable orders or bypass production risk controls. Feed/intent inputs are explicit
caller-supplied test evidence, not verified real-feed/account certificates. The
storage-ownership observation cannot fence in-flight venue orders.

### Actual simulated-lifecycle rehearsal

A new test uses the project's real `TradeLifecycle`, real risk-engine fixture,
fake Redis event bus and `SimulatedOrderExecutor`, without changing risk settings:

1. Submit a synthetic opportunity and receive an opened paper Trade.
2. Persist the complete typed snapshot to a temporary journal.
3. Reopen the file and create a new lifecycle object.
4. Hydrate its open-state map **only in test code**; no runner hydration hook added.
5. Close through the real lifecycle using the simulated executor.
6. Persist the terminal revision and verify open state is empty and closed PnL is
   retained on recovery.
7. Verify a later differing terminal snapshot is still rejected, not silently
   treated as an approved financial amendment.

This is an in-process/object-restart rehearsal, not a production process restart,
real-service transport test, paper-market soak or real-account close.

### Read-only legacy assessment

`assess_legacy_rows` accepts a bounded supplied dataset; it does not connect to any
DB, import records or mutate inputs. It detects incomplete rows, identity/schema
issues and retained millisecond-key payload conflicts. Missing scope is reported.
`history_not_attested` is always reported: merged-away conflicts cannot be inferred
from survivors. `eligible_to_import=False` and `live_authorized=False` always.
This is a triage report, not provenance attestation or a migration utility.

## Final verification

| Check | Actual result |
|---|---|
| New binding/timestamp/recovery tests | **36 passed, 0.25s** |
| Binding + previous journal unit/process tests | **73 passed, 5.63s** before final readiness naming refinement; final full run covers final source |
| Full backend nonintegration | **1856 passed, 32 deselected, 67.93s** |
| Separate embedded safeguards/hazards/projection | **16 passed, 1.33s** |
| Frontend | **64 passed** |
| Production TypeScript/Vite build | VERIFIED successful |
| npm audit | VERIFIED **0 vulnerabilities reported** at this run |
| Nine-page Chromium | VERIFIED pages/planner/export/performance/readiness/failure clearing/mobile/no JS errors |
| Built-gateway authentication Chromium | **1 passed, 12.26s** |
| Black / F-lint / Bandit / compile / whitespace | VERIFIED focused checks successful |
| Persistence CI | Added binding/model paths and typed tests; remote execution NOT TESTED |
| Independent review | NOT TESTED; no separate reviewer/subagent was available |

Counts overlap. The 36 new binding tests are included in the backend total; the
legacy hazard tests intentionally demonstrate still-unresolved active-schema
limitations. Tests use fake/synthetic financial inputs and test-only credentials.

Logs: ignored `tmp/paper-binding/backend-final.log`, `embedded-final.log`,
`frontend-build.log`, `auth-browser-final.log`. Source and tests are the primary
reproducible deliverables; temporary logs are not required by the application.

## Measured local microbenchmark — not production latency

Command: `.venv/bin/python engineering/tools/benchmark_paper_revision.py`.
Output: ignored `tmp/paper-binding/benchmark.json`.

Python 3.11.2, SQLite 3.40.1, Linux sandbox filesystem, WAL/FULL, single writer,
`perf_counter_ns`. Workload: 20 synthetic trades × 5 revisions = 100 timed typed
appends; 50 timed complete recoveries including validation of 100 historical events
and decoded copies of 20 typed open heads. No warmup subtraction.

| Stage | Samples | p50 ms | p95 ms | p99 ms | Max ms |
|---|---:|---:|---:|---:|---:|
| Typed append | 100 | 2.142 | 2.668 | 3.411 | 3.938 |
| Complete typed recovery | 50 | 10.213 | 10.598 | 10.966 | 10.966 |

**NOT MEASURED:** production p50/p95/p99, contention/capacity, real feed-to-decision/
order latency, network sink lag, real-service restarts and power-loss resilience.
No integrity checks or calculations were removed. The script creates and removes
only its own temporary synthetic DB and cannot target an existing journal.

## Completion ledger and remaining release gates

| Work item | Current status | Next required evidence / dependency |
|---|---|---|
| Bounded quant/ledger analytics and desktop views | Implemented, current regression passing | Real data quality/account-equity validation remains separate |
| Operator gateway and session UX | Implemented, built-browser verified | Nonlocal TLS/security review, identity/MFA/native secret handling |
| Scoped CAS/fenced storage prototype | Implemented, isolated process/crash tests passing | Independent review; execution-ownership binding; no active cutover |
| Typed paper model/recovery/monitoring rehearsal | Implemented here, simulated lifecycle verified | Corrupt-portfolio emergency monitoring design, approved runner integration |
| Legacy assessment | Read-only bounded assessment implemented | Explicit account provenance, inventory/export, reconciliation before import |
| Terminal fee/funding amendments | NOT IMPLEMENTED; differing terminal writes rejected | Reviewed immutable amendment event/receipt/linkage and accounting contract |
| Active outbox projector | NOT IMPLEMENTED; test-only projection verified | Durable delivery worker, cross-batch sink checks, lag/capacity/fault tests |
| Real paper runner restart/soak | NOT TESTED | Separately reviewed wiring and isolated real-service environment |
| Real-account reconciliation / exchange-order fencing | NOT TESTED / not implemented by this storage fence | Approved read-only venue evidence and execution-level ownership review |
| Production migration/activation | NOT EXECUTED | Explicit cutover approval, backups, verified import and reconciled rollback |
| Native vault, signed installer/updater, multi-OS delivery | NOT IMPLEMENTED/certified | Platform design, secret-store/signing/update/recovery work and OS tests |
| Audited mutation/job-launch control plane | NOT IMPLEMENTED | Authorization/audit/idempotency/cancellation contracts and security review |
| Evaluated LLM integration | NOT IMPLEMENTED | Provider/privacy/evaluation/budget design; never substitute it for financial gates |
| Independent security/quant/operational certification | NOT TESTED | Actual independent review and production-like evidence; no ranking/edge claim |

These remaining items cannot honestly be relabelled complete from local unit tests.
The user's completion request is not treated as permission to submit real orders,
change risk/strategy, deploy or destructively migrate financial state.

## Rollback / safe handoff

The new paper adapter is not imported by any application runner. Ceasing to use
its explicit temporary file leaves current storage authority unchanged. The
serializer patch is additive; reverting it reintroduces the reproduced timestamp
loss and is not a recommended recovery technique. Do not backfill legacy history
with invented timestamps or drop conflicting rows to get a green readiness result.

Review the complete source/test diff before any activation. The next engineering
priority is immutable financial amendment and execution-ownership contracts plus
independent review, before active projector/runner wiring. Production cutover and
live trading remain separate gates, not implied by this implementation batch.
