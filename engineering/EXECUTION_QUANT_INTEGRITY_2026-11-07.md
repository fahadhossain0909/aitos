# Execution intent ownership and quant numerical integrity — 2026-11-07

## Scope and decision

Fourth backend-first package on `arena/ed127d35-aitos`, HEAD `6f8052bb`.
Existing unrelated/prior working-tree changes preserved. No commit, push, deployment,
exchange call, real order, database migration, live activation or risk/strategy threshold
change. This package is NOT a claim that all remaining backend work is complete.

Two production paths improved: `IdempotentOrderExecutor` / lifecycle entry identity,
and candidate / direct walk-forward research validation. These are wired existing
components, not an unused alternative execution or research framework.

## Findings and implemented changes

| ID | Severity | ROOT CAUSE CONFIRMED | Implemented behavior |
|---|---|---|---|
| AUD-20261107-18 | P1 execution reliability | Awaiters directly owned/cancelled the shared order submission; cache/removal belonged to the first caller | One task per in-process identity; every waiter uses `asyncio.shield`; task completion owns cache/removal and retrieves exceptions even with no remaining caller |
| AUD-20261107-19 | P1 execution integrity | Explicit client IDs had no request-payload binding; fallback omitted reduce-only and rounded economic values; entry path did not supply logical opportunity identity | Conflicting ID reuse raises before submission, including after failed/cached submission; fallback includes reduce-only and normalized `float.hex` values; lifecycle entry uses `aitos-entry-` + 24 hex SHA-256 characters from opportunity ID (36 characters) |
| AUD-20261107-20 | P1 research integrity | NaN bypassed threshold comparisons; nonfinite thresholds, bool/fraction counts and malformed metrics were accepted | Finite policy thresholds; integral non-bool counts; shared metric validation for development, champion, every walk-forward window and locked holdout; reject missing/non-numeric/bool/nonfinite evidence, negative drawdown and invalid trade counts |
| AUD-20261107-21 | P1 research availability | Direct walk-forward API accepted zero/negative/fraction durations; nonpositive test duration could make its cursor never advance | Validate positive integral durations before iteration; nonnegative integral minimum-window count; aware ordered boundaries; timestamp-awareness validation before sorting prevents mixed-timezone TypeError |
| AUD-20261107-22 | P1 execution input integrity | Wrapper registered/submitted malformed economics; NaN bypasses ordinary positivity comparisons | Before key registration/cache lookup/venue call, quantity and reference price must be real, finite and positive; bool/string/None rejected without poisoning the valid identity |

### Execution contract boundaries

The wrapper remains **in-process deduplication**, not a durable/exactly-once order ledger.
No process-restart, multiple-process or exchange-uncertainty guarantee is implied.
Fallback economic identity cannot distinguish two intentional equal-economics orders;
callers must supply logical IDs. Entry now does; exit/resting-order identity and durable
reconciliation need separate coordinated work. Hash identity is not a cryptographic
provenance attestation. Same opportunity with changed sizing/reference economics fails
closed rather than returning an unrelated cached fill.

Shielding is deliberately local: cancelling a waiter does not cancel the venue coroutine,
and task success is cached even if every waiter leaves. **That does not mean lifecycle,
capital, persistence or protective-order state has completed.** The capital wrapper's
finally block can release a reservation when the enclosing lifecycle is cancelled,
while the shielded venue operation still runs. This broader uncertainty already matters
for network cancellation/timeouts, and remains a live-activation blocker. Underlying
submission failures remain retryable with the same payload; exchange status must be
reconciled before uncertain real submissions can be safely retried. Request/completion
maps remain unbounded, as before; no shutdown-drain or restart guarantee was added.

### Research contract boundaries

`aitos/learning/metric_integrity.py` validates the canonical four decision metrics:
`total_return`, `max_drawdown`, `sharpe`, `trades`. It does not invent returns or convert
strings/missing values into good evidence. Return magnitudes/drawdowns are not newly
capped here (leveraged losses may exceed equity); strategy thresholds remain unchanged.
Existing governance retains its additional six-field evidence digest contract.

On the development comparison path, invalid development/champion evidence stops
before OOS evaluation; invalid window
evidence stops before consuming locked holdout. Valid holdout separation, frozen
strategy copies and the existing backtest engine remain unchanged. Passing finite
Sharpe is not annualization correctness, significance, leakage-proof arbitrary external
strategy state, a profitability claim or live approval. Direct walk-forward naive and
mixed-aware timestamps now raise a clear ValueError before sorting; CandidateValidator's
existing event ingestion is otherwise unchanged.

## Reproductions and verification

### RED evidence

- Intent ownership/identity: **11 failed, 1 passed in 30.27s**, `tmp/order-intents-red.log`.
- Numerical policies/evidence/direct durations: **41 failed, 3 passed in 2.28s**,
  `tmp/numerical-integrity-red.log`; dangerous duration reproduction bounded by a
  one-second pytest timeout to prevent a hanging test process.
- Extra string-evidence/mixed-timestamp regressions: **5 failed, 65 passed in 0.25s**,
  `tmp/numerical-integrity-extension-red.log`.
- Invalid order economics: **16 failed, 13 deselected in 0.24s**,
  `tmp/order-economics-red.log`.

### GREEN evidence

- **99 new tests passed in 0.41s**, RuntimeWarning treated as error:
  `tests/test_order_intent_ownership.py` (29) and
  `tests/learning/test_validation_numerical_integrity.py` (70).
- Earlier combined execution/lifecycle suites: **50 passed in 1.10s**.
- Earlier learning suites after initial numerical patch: **91 passed in 0.34s**.
- Intermediate non-integration suite (before final additional cases):
  **1434 passed, 15 deselected in 65.57s**.
- Final non-integration suite: **1477 passed, 15 deselected in 67.71s**, no warnings;
  log `tmp/execution-quant-final.log`. The 15 exclusions are six separately exercised
  Redis cases and nine older service-backed cases, not a full-suite certification.
- Final existing execution/lifecycle + learning regression selection: **155 passed
  in 1.40s**. No Redis process remained after the isolated test rerun.
- Real isolated Redis regression rerun: **6 passed in 0.90s**, private Unix socket,
  TCP disabled, test-owned AOF/data and explicit existing local Redis 7.2.7 binary.
  No existing database/account used. This is not Binance/position recovery evidence.
- Repository Black 25.12.0: **599 files unchanged**.
- Five changed production files: compileall, selected Flake8 (`E9,F63,F7,F82`) and
  Bandit medium/high checks **PASS**. `pip check` and `git diff --check` **PASS**.

Reproduce:

```sh
.venv/bin/python -m pytest -q tests/test_order_intent_ownership.py \
  tests/learning/test_validation_numerical_integrity.py -W error::RuntimeWarning --timeout=30
.venv/bin/python -m pytest -q -m 'not integration' --timeout=60
AITOS_TEST_REDIS_SERVER="$PWD/tmp/redis-build/redis-7.2.7/src/redis-server" \
  .venv/bin/python -m pytest -q tests/integration/test_isolated_redis_recovery.py --timeout=30
.venv/bin/python -m black --check .
```

## Remaining activation gates and next implementation sequence

1. **P0 AUD-20261107-17 remains OPEN:** current Binance NEW/zero-executedQty path can
   report the requested full fill. Existing offline reproduction remains applicable;
   executor result construction was deliberately not partially patched here.
2. Next coordinated change must cover pre-submit durable logical intent + payload
   fingerprint, accepted/unknown/partial/filled/rejected/cancelled transitions,
   capital lease retained while exposure is uncertain, and restart reconciliation by
   client order ID before resubmission. Submission caller cancellation must not sever
   ownership of lifecycle/protection/persistence. Pending/partial exposure needs actual
   protective-order monitoring, not just a pending exception or background task.
3. Required local fault matrix: accepted+timeout, NEW with zero fill, partial then
   terminal fill/cancel, duplicate delivery, conflicting client ID, protection failure,
   caller/process cancellation, restart before/after submit/record/protect, failed
   journal write, unavailable status lookup. Real-service evidence must remain
   separately labelled from mocked venue tests.
4. Nine older service-backed integration cases remain NOT TESTED locally. Six Redis
   cases are exercised separately, not proof of ClickHouse/Neo4j/account recovery.
5. Python matrix/remote CI and independent review: NOT TESTED. Production-path latency,
   throughput and paper soak: NOT MEASURED / NOT TESTED. Desktop/frontend stays deferred
   until backend readiness; passing unit suites are not permission to activate live.

## Rollback

Do not reset this broad working tree. Revert only this package's hunks: task ownership,
request binding/numeric guard and fallback key in `persistent_state.py`; opportunity
client ID plus hashlib import in `lifecycle.py`; numerical gates/imports in
`validation.py` and `walk_forward.py`; new metric helper and its two dedicated test files.
Preserve prior status forwarding, holdout isolation, resource ownership changes and
unrelated founder changes. Re-run targeted + broad isolated tests after any rollback.
Restoring old client-ID/cache behavior reintroduces the reproduced defects and is not
an approved live mitigation.
