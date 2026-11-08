# Immutable fee/funding amendments — 2026-11-06

## Outcome / operating mode

**VERIFIED:** the unwired paper revision prototype now supports explicit immutable
closed-trade fee/funding amendments. Each command appends a new full snapshot with
parent linkage and receipt metadata; the original close and all prior events
remain unchanged. No active runner/projector, exchange request, deployment,
production DB migration, strategy/risk/Redis setting change, commit or push.

Branch `arena/ed127d35-aitos`, baseline `6f8052b`; broad existing work preserved.
This increment implements a previously missing **prototype contract**, not a
production financial amendment service or account reconciliation certificate.
Existing production lifecycle/persistence behavior is not switched to this path.

## Contract and exact changes

- `aitos/trading/financial_amendment.py`: pure stdlib `CostAmendment`, strict
  versioned metadata validation and deterministic fee/funding delta calculation.
- `aitos/trading/revision_journal.py`: explicit `amend_closed`, shared atomic
  insertion, amendment lineage replay and authoritative scoped receipt checks.
- `aitos/trading/paper_revision.py`: explicit paper `amend_cost` adapter; typed
  decoding recognizes this one metadata extension without discarding unknown fields.
- `tests/test_financial_amendments.py`: 32 isolated contract, accounting, receipt,
  corruption, process-race and process-death tests.
- `tests/test_revision_journal_projection.py`: added real embedded append-only
  projection of original close + amendment + duplicate redelivery.
- `.github/workflows/persistence-quality.yml`: relevant paths and amendment tests
  added to existing no-migration verification job; remote execution NOT TESTED.

No new tables, automatic schema migration, API endpoints, background tasks,
configuration switches or application imports were added. Metadata is a known
payload extension, `_financial_amendment_v1`, while the existing envelope and
projection columns remain unchanged. Older prototype readers do not understand
this post-terminal lineage and should fail closed; do not reopen amended files
with old builds as an operational rollback.

### Command identity and receipt evidence

Required inputs: current paper Writer token, trade ID, stable event ID, expected
parent revision **and digest**, and immutable command fields:

- trusted source namespace and stable adjustment ID;
- kind `fee` or `funding`;
- finite amount in USD terms (bool/string/nonfinite rejected);
- explicit timezone-aware recorded-at timestamp;
- SHA-256 reference to an **individual cost receipt**, not a whole batch response.

Fees are nonnegative costs; funding is signed cashflow. Zero fees and tiny fees
remain in receipt/history evidence even when net-PnL display rounding is unchanged.
Fee refunds/reversals, gross-PnL rewrites, quantity/price corrections, currency/FX
conversion and historical-fill repairs are not supported by this command.

The receipt hash is a reference, not provider authentication. No real receipt was
fetched or verified. Sources must eventually bind external IDs, account, currency,
conversion, kind and amount to trusted venue evidence; callers must not treat an
arbitrary digest as financial authorization. Raw responses/secrets are not stored.
The same hash must not represent multiple cost events from a batch; use separate
canonical receipt fragments including the individual external identity and kind.

### Transaction and retry rules

Within one SQLite `BEGIN IMMEDIATE` transaction:

1. Validate current writer generation. A stale writer cannot append or obtain an
   accepted duplicate acknowledgement through the command.
2. Validate current head/history and scoped receipt uniqueness.
3. If the event already exists, require the same trade, command bytes, original
   parent revision and digest. Return the original event plus current head; do not
   overwrite a newer head. Changing an amount/source/reference under that event
   ID is a conflict.
4. For a new event, CAS the parent revision/digest. Only valid complete **closed**
   evidence is eligible. No automatic latest-head retry or arbitrary replacement
   snapshot is accepted.
5. Reject reuse of `(scope, source, adjustment_id)` or any receipt digest within
   the scope, including another trade/source label. A receipt already in a Trade's
   adjustment map is also rejected rather than charged again without provenance.
6. Calculate the exact allowed delta, append the new event, update the head and add
   the outbox row in the same transaction. Commit before returning acknowledgement.

Receipts can be repeated across **different trusted account/run scopes**. That is
scope separation, not proof that the caller chose the correct real account.
Baseline costs lacking receipt provenance cannot automatically be matched against
later venue income; this prototype does not solve that reconciliation problem.

### Accounting and lineage

The new snapshot may change only cost-adjustment evidence, fees, funding, recorded
net PnL, net percentage and update time. Gross realized PnL, original position,
entry/exit evidence, side, strategy, partial exits and other fields stay unchanged.
The new metadata names the immediate prior revision/digest and receipt command.

Calculation matches existing `TradeLifecycle.apply_cost_adjustment`: positive
fees subtract, signed funding adds; net PnL and amendment percentage use the same
8-decimal rounding and initial-notional denominator. A canonical full-snapshot
comparison against that real simulated lifecycle is tested for fee, funding paid
and funding received. Existing lifecycle/risk semantics were not modified.

This retains the repository's binary-float accounting, not an exact-decimal money
certificate. Overflow is rejected atomically. Production precision/currency policy
and real settlement reconciliation still need review.

Recovery checks both parent and amended Trade schemas, exact allowed delta,
parent reference, contiguous history and outbox references. Invalid parent evidence
cannot be washed into a valid-looking derived head, even if payload hashes are
recomputed. Complete scoped recovery/outbox reads reject duplicate authoritative
receipt identities/digests. Hashes cannot prevent a privileged host rewriting all
its evidence; this is not a tamper-proof audit against the host owner.

The ordinary append/save API still rejects differing post-terminal snapshots and
reserved amendment metadata. It cannot reopen a position or bypass the explicit
command. Replaying events never calls an exchange or updates risk configuration.

## Projection / analytics behavior

The embedded MergeTree test retains original close, amended close and an exact
redelivery. Bounded projection verification accepts exact duplicates only; parent
linkage stays in the immutable payload. Local outbox ACK follows successful test
verification. No actual network projector is started.

Analytics must use the latest scoped authoritative head, **not sum all historical
closed snapshots**. A regression confirms recovered latest heads yield amended
net PnL exactly once; feeding original + amended snapshots with the same trade ID
to existing analytics is rejected as duplicate data.

Projection batch checks are not complete sink-history certification or cross-
database exactly-once delivery. An active projector/reader needs separate cross-
batch sink checks, ordering/idempotency, ownership, capacity and fault evidence.

## Executed verification

| Check | Observed result |
|---|---|
| Dedicated amendment suite | **32 passed, 6.11s** |
| Full backend nonintegration | **1888 passed, 33 deselected, 70.57s** |
| Separate embedded safeguards/hazards/projection | **17 passed, 1.99s** |
| Frontend | **64 passed** |
| Production build / npm audit | VERIFIED success / **0 findings reported** |
| Focused Black/F-lint/Bandit/compile/whitespace | VERIFIED successful |
| Workflow YAML/permissions and no application imports | VERIFIED local checks |
| Dependency check | VERIFIED no broken requirements |
| Remote CI / independent review | NOT TESTED |
| Existing nine-page/auth browsers | NOT TESTED again this increment; prior results historical |

Counts overlap: the 32 new amendment cases are in the backend total. The one new
embedded projection case increases the separate suite from 16 to 17 and deselected
integration cases from 32 to 33. Existing legacy hazard tests still characterize
unresolved active-schema weaknesses; this increment does not fix deployed storage.

Separate-process tests demonstrate two competing receipts on one parent have one
winner, and identical concurrent retries commit one cost plus a duplicate ACK.
Actual process exits during the outbox insertion roll back the entire amendment;
exit after commit preserves it and retry does not double-apply the cost. These are
process-death tests, not power-loss/storage-controller/disk-full certification.

Focused author consistency review added invalid-parent and cross-trade receipt
corruption regressions. This was **not independent review**, and is not represented
as such. No additional production incident is claimed.

Commands:

```sh
.venv/bin/pytest tests/test_financial_amendments.py --timeout=90 -q
.venv/bin/pytest -q -m 'not integration' --timeout=90
.venv/bin/pytest tests/test_clickhouse_embedded_ledger.py \
  tests/test_clickhouse_revision_hazards.py \
  tests/test_revision_journal_projection.py -m integration --timeout=90 -q
```

Ignored logs: `tmp/financial-amendments/focused-final.log`, `backend-final.log`,
`embedded-final.log`, `frontend-build.log`. Environment remains Python 3.11.2,
SQLite 3.40.1, chdb 4.4.0 / embedded ClickHouse 26.9.2.1. All inputs and receipt
references are synthetic; there are no real account credentials or funds.

## Remaining gates / rollback

- Prototype fee/funding amendments: **implemented and locally verified**.
- Production amendment integration, baseline/venue receipt reconciliation,
  provider evidence and money precision policy: **not implemented/certified**.
- Active projector/runner integration and real-service restart/soak: still open.
- Exchange-order fencing/account reconciliation, independent review and native
  vault/signed installer/updater: still open.
- Production deployment/migration/live activation: not executed or authorized here.

No production latency/capacity or real feed/venue/account health was measured.
Scoped receipt checks currently scan retained events; history replay is O(history).
Do not omit financial checks to make a benchmark look better. Indexing/operational
bounds and representative contention/soak evidence precede any active cutover.

There is no runtime cutover to roll back. Stop the isolated tests/cease using their
explicit temporary file. Preserve amended histories and never overwrite/delete a
close to simulate rollback. Do not run old readers on new amended histories or
clear receipts/financial state to obtain a passing readiness signal. Review source,
contracts and evidence before separately approved activation.
