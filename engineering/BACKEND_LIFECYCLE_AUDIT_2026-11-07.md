# Backend lifecycle safety — follow-up implementation, 2026-11-07

## Scope and state

Continuation of `BACKEND_FIRST_ROADMAP.md`, package B1 startup/resource ownership.
This is NOT completion of financial lifecycle, exchange reconciliation or backend
certification. All failure injection uses fake clients; no real orders, migrations,
Redis tuning, external service startup, deployment or user-data changes.

Branch: `arena/ed127d35-aitos`. At this continuation the persisted workspace presents
Git HEAD `6f8052bbc9818bda83f6a566cd70aac82ccb4c46` with the previously reviewed newer
main content and first audit changes as working-tree diffs/untracked files. Existing
changes were preserved; no reset, branch switch, history rewrite or dirty-tree merge
was attempted. The previous report's `040fff9c` observation is retained as historical
session evidence, not falsely claimed as the current HEAD.

The ignored `.venv` had not persisted and was recreated using both requirements
profiles plus Black. Python 3.11.2. `pip check` passes. Docker executable is absent;
there is no available local ClickHouse service to certify real-service recovery.

## Findings and root causes

### AUD-20261107-06 — Failed repository initialization leaks owned clients

Severity: P1 reliability. Status: ROOT CAUSE CONFIRMED; isolated regression-verified.
Affected: `MarketDataRepository`, `JournalRepository`, `DecisionJournalRepository`.

Each repository assigned its acquired async ClickHouse client to `self._client` and
then awaited schema operations without rollback ownership. DDL failure or cancellation
left that client open; a retry could overwrite it with another acquired client. Runner
cleanup after final failure cannot close clients already overwritten by earlier retries.

Fix:
- Scope schema setup in `try/except BaseException` to include task cancellation.
- Attempt repository shutdown before rethrowing the original startup failure.
- Log cleanup failure by exception type only, without connection credentials/payloads.
- Preserve the client handle on failed close; retry closing it before acquiring a new one.
- Do not publish initialized state until all original initialization operations complete.

No DDL/query/schema was added, removed or reordered. Journal's existing legacy timestamp
repair remains unchanged and was NOT executed against a database in this work.

### AUD-20261107-07 — Closed repository remains logically initialized

Severity: P1 reliability. Status: ROOT CAUSE CONFIRMED; isolated regression-verified.
Affected: the same three repositories.

Shutdown closed the client without clearing readiness or its reference. Later initialize
returned early because `_initialized` was still true; write guards could pass against
an already-closed client. Repeated shutdown could close the same handle repeatedly.

Fix: clear readiness before awaiting close. Clear the client reference only after close
succeeds. Repeated successful shutdown is a no-op; explicit reinitialization acquires a
fresh client. A failed close leaves writes disabled and retains the handle for retry.

Limits: serial lifecycle ownership is assumed. This patch does not introduce locks for
concurrent initialize/shutdown, drain in-flight queries, or shield cleanup against repeated
external cancellation. Those require separate coordination/time-budget design.

### AUD-20261107-08 — Composition-root subscription ownership gap

Severity: P2 reliability. Status: ROOT CAUSE CONFIRMED; isolated regression-verified.
Affected: `aitos.app.initialize_all`.

Price/cost subscription handles were assigned via one list expression. If the second or
later subscription raised, successfully acquired earlier handles were absent from the
registry used by `shutdown_all`. The same cost-adjustment route/group was also registered
twice. A redundant subscription does not itself prove double ledger application: delivery
semantics/idempotency still need dedicated evidence.

Fix: append each successfully returned subscription immediately. Register the two price
routes and one cost-adjustment route once each. Existing runner-managed shutdown retains
ownership on partial failure. Module order, groups, topics and handler semantics unchanged.
No event schema, worker tuning or live subscription activation was performed.

## Reproduction and tests

New tests before production fixes:
- Repository lifecycle: **18 failed, 3 passed** in 0.68s.
- Subscription ownership: **2 failed** in 0.12s.

After fixes:
- `pytest -q tests/test_app_subscription_ownership.py tests/data/test_repository_lifecycle_safety.py tests/data/test_repository_live_persistence.py tests/test_decision_journal.py tests/test_runtime_connections.py tests/test_runtime_startup_cleanup.py --timeout=30`
- **35 passed in 0.79s**.

Repository cases cover first/second schema command failure, readiness guards, normal
reinitialize, repeated shutdown, cancellation, cleanup error preserving the original
failure, retained-handle cleanup retry and connection creation failure. Subscription
cases check exact route/group registration and shutdown ownership after partial failure.
Mock schema commands cannot certify ClickHouse SQL validity or real transport closure.

Broader regression and formatting: recorded below after execution, not inferred.

## Changed files in this continuation

Production:
- `aitos/data/repository.py` — acquired-client rollback/readiness lifecycle.
- `aitos/journal/repository.py` — same, including unchanged schema-repair path.
- `aitos/journal/decision_repository.py` — same; prior management query changes preserved.
- `aitos/app.py` — incremental subscription ownership and redundant route removal.

Tests:
- `tests/data/test_repository_lifecycle_safety.py` — 21 failure/restart cases.
- `tests/test_app_subscription_ownership.py` — 2 composition-root ownership cases.

Documentation: this report and append-only progress/register/verification updates.

## Operational gates / rollback

Status: FIX IMPLEMENTED — NOT VERIFIED in the real paper stack. Unit/failure injection
is VERIFIED; actual Redis/ClickHouse restart/transport behavior remains NOT TESTED.
Latency, profitability and durability under process termination remain NOT MEASURED.

Do not deploy or activate this package without reviewing the final combined diff and CI,
then running the approved isolated paper stack tests. No independent reviewer was available.
Rollback only the lifecycle/subscription hunks and their new tests from this continuation;
retain prior main/audit/user changes. No database/data rollback required.

Next: resource ownership in infrastructure retry helpers; ambiguous order/ACK reconciliation,
restore/monitor continuity and service-backed lifecycle tests. Frontend remains deferred.

## Verification follow-up and test classification

Before marker correction, broad non-integration run: **1346 passed, 7 failed,
2 deselected, 9 warnings in 69.36s**. The same seven app-wiring tests attempted the
absent local ClickHouse server. Their real-service requirement is now explicit:
`tests/test_app_wiring.py` marks just those seven as `integration`, with no skips,
xfails or mock substitution. Collection verified **7/12 selected**, 5 deselected
for `pytest --collect-only -q -m integration tests/test_app_wiring.py`.
The full CI command has no marker exclusion and still exercises these tests with
its ClickHouse service. No full CI or real-service passing result is claimed here.

Additional changed file: `tests/test_app_wiring.py` (documentation and classification
only). This supersedes the earlier report's unclassified-test note prospectively,
without erasing its recorded failures. Use:

```sh
# Service-free regression selection
.venv/bin/python -m pytest -q -m 'not integration' --timeout=60
# Real ClickHouse app wiring; run only on approved isolated test infrastructure
.venv/bin/python -m pytest -q -m integration tests/test_app_wiring.py --timeout=60
```

Changed-file Black check: **PASS, 7 files unchanged**. `pip check`: **PASS**.
`git diff --check`: **PASS**. Final isolated-run result follows after observed execution.

Final isolated broad run: **1346 passed, 9 deselected, 2 warnings in 63.73s**.
The nine deselections are seven newly classified ClickHouse wiring cases plus two
previously marked integration cases. This is a successful non-integration selection,
NOT a full real-service suite pass. Remaining warnings are the reproducible unawaited
`_watchdog` coroutine in two synchronous EventBus construction tests; source points to
coroutine creation before checking for a running loop. It is not repaired in this package.
Changed production files also pass `compileall` syntax validation.
