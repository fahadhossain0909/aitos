# Main comparison and integration evidence

## Executive summary / mode

**VERIFIED:** fetched main `e332e2b9fbdcd0e10bef9a86fd557e0750fb9a4f`
is an ancestor of integration commit
`58807516c519372bb8b33a8c94caf421cd313de3` on the required branch
`arena/ed127d35-aitos`. No main checkout, push, merge into main, force push,
production deployment, live activation, real exchange order or destructive migration.

The user explicitly requested comparison against advanced main and integration of
necessary changes. This is a source integration and isolated verification, not
activation of main's new strategies, production configuration or financial authority.

### Reproducible source comparison

- Clean starting branch HEAD: `38bdc30458bb8fef61a3702a00eb7530631c8939`.
- The original checkout was shallow; initial three-dot diff had no merge base.
  `git fetch --unshallow origin main` restored history without rewriting it.
- Actual merge base: `6f8052bbc9818bda83f6a566cd70aac82ccb4c46`.
- Reachability comparison after full fetch: 5 branch-only / 877 main-only commits
  (includes merged feature-branch history, not 877 distinct linear source changes).
- Main-side net diff: 168 files; overlap with this branch: 57 files.
- Actual content/add-add conflicts: 13 files, resolved individually, not a blanket
  ours/theirs selection. Final merge changes 132 files relative to old branch HEAD.
- Merge parents: old branch `38bdc30` and fetched main `e332e2b9`.
- Remote main was checked again after integration and still matched the fetched SHA.

## What came from main

- Durable submission/acknowledgement ledger, client-ID recovery, startup blocking
  for ambiguous intents and acknowledged orders without a durable trade snapshot.
- Operational PostgreSQL market/journal adapters, capability-aware backend selection,
  explicit analytics limits and bidirectional PostgreSQL/ClickHouse reconciliation.
- Updated Financial ML/statistical validation, risk evidence, candidate/outcome
  attribution, scanner/position analytics, A-Stat feedback and optional drift monitor.
- Updated upstream tests, docs, runtime dependencies (`asyncpg`, `scipy`) and CI.
- Main's resting-order prohibitions for cTrader/MT5 remain in place.

Existing branch-only desktop/gateway/native vault, account evidence, default-off
paper mirror, append-only prototype/projector, repeated-cancellation hardening and
continuous-unique-event service harness were retained. No historical records/IDs
were rewritten and no persisted authority was switched.

## Conflict resolution / dependency map

| Area | Integration decision and evidence |
|---|---|
| `aitos/app.py` | Keep new A-Stat feedback component and main wiring alongside branch-owned startup/cleanup safeguards. App wiring regression suite passes. |
| `binance_executor.py` | Preserve strict terminal quantity/price/identity validation and uncertain-execution latch, while retaining main client-ID lookup. Main zero/partial-fill fixtures now provide authoritative pre/post-cancel GET evidence instead of treating cancellation ACK alone as final proof. |
| `ctrader_executor.py`, `mt5_gateway.py` | Combine branch reduce-only fail-closed guards with main MARKET-only restrictions; do not re-enable unmonitored resting orders. |
| `scanner.py` | Keep main scanner/statistical features and required `log` import; nonconflicting branch telemetry/task ownership changes remain. |
| `experiment_engine.py` | Keep main return-series/family validation/report fields plus branch deep-copy cell isolation, cost-name uniqueness and sample-policy identity. |
| `validation.py` | Keep finite/count/metric checks and frozen candidate deepcopy, without removing main's nonconflicting new validation features. |
| `runtime_connections.py` | Keep main capability-aware PostgreSQL selection; restore independent cleanup attempts for journal-selection failure/cancellation and cleanup of non-retry market initialization failures. Preserve Redis/Neo4j attempt ownership. |
| `persistent_state.py` | Keep main durable intent/ACK reconciliation and new-order blocking; preserve branch in-memory request binding, shielded submission-owned settlement, partial-fill caching and explicit status delegation. |
| `lifecycle.py` | Keep actual-fill accounting/candidate attribution, strict live execution evidence and persist-before-telemetry behavior. Stable opportunity-derived trade ID becomes the exact `aitos-entry-` client suffix, satisfying BOTH branch retry identity and main's orphan-order suffix lookup. Closed/outcome attribution and live commit both retained. |
| `run_paper_trading.py` | Keep drift shutdown and main successful-shutdown marker; put drift shutdown inside the shadow-drain `try/finally`, so even cancellation there cannot bypass mirror cleanup. |
| `docs/CONTINUOUS_INTELLIGENCE.md` | Retain main docs and branch frozen-input/provenance limitations; do not silently drop either. |
| `tests/learning/test_experiment_rollout.py` | Keep both earlier cell isolation/policy regressions and main family-testing regression. |

### Failures observed and corrected

**REPRODUCED:** first integrated run failed 8 tests before maxfail: two main Binance
fixtures lacked the stricter branch read-back sequence; six journal-startup cleanup
cases exposed lost cancellation/independent cleanup or changed error layering.
The next full run found seven further compatibility failures: logical entry ID,
five AST teardown fixtures missing drift/logger globals, and non-live zero-fill
rejection versus live uncertain-execution handling.

**ROOT CAUSE CONFIRMED:** main derives orphan ownership from the client-ID suffix,
but random new trade IDs broke branch stable opportunity retry identity. The
integrated identity is deterministic per opportunity and client suffix equals
actual trade ID. Regression explicitly verifies both properties and distinct
opportunity separation. Existing historical IDs are untouched.

Tests were not skipped to resolve conflicts. The journal-failure fixture now
expects main's sanitized `StorageBackendError` wrapped in `SystemExit` for ordinary
failed capability probes; cancellation still must be the original exception and
both repository close attempts are asserted even when either cleanup fails.
New drift failure/cancellation cases execute the actual runner teardown AST.

## Actual local verification (integrated source)

| Check | Observed result |
|---|---|
| Full backend `pytest tests -m 'not integration' --timeout=90 -q` | **2270 passed, 2 skipped, 38 deselected; 80.80s** |
| Focused Binance/infrastructure/backend selection | **52 passed** |
| Focused lifecycle/identity/live application/teardown/reliability | **88 passed** |
| Embedded SQL / hazards / revision projection / service harness | **22 passed; 23.94s** |
| Frontend | **84 passed**, production build PASS |
| Built authenticated Chromium/gateway | **1 passed; 11.92s** |
| Shared native host + vault on Linux | **12 passed, 1 Windows-only skipped** |
| Whole repository Black | **735 files unchanged**, PASS |
| Python compile / YAML parsing / whitespace | PASS |
| Whole repository Flake8 F-lint | **17 inherited findings**, not a clean whole-tree result; compared normalized file/message identities with BOTH parent versions: **zero new findings** |

The two local backend skips are the real PostgreSQL round-trip and PostgreSQL-to-
ClickHouse tests: their service DSNs are absent locally. They are not represented
as local passes. A separate real hosted service job below supplies those DSNs.
The Linux native skip is not Windows proof; hosted Windows validation is separate.
No representative production latency/capacity percentile was measured.

## Hosted verification — exact integrated source 58807516

Manual isolated validation runs were selected for consistent evidence. Push runs
appeared after the initial API listing; explicit dispatch overlapped them. Five
push duplicates were superseded by workflow concurrency, and the remaining two
Windows/service push duplicates were explicitly cancelled to avoid duplicate cost.
These cancellations are not failing tests and are not claimed passes. The selected
manual runs below are the acceptance evidence.

| Workflow | Status / evidence |
|---|---|
| PostgreSQL adapter + bidirectional ClickHouse reconciliation | **PASS**, https://github.com/fahadhossain0909/aitos/actions/runs/42645638096 |
| Full backend / persistence / embedded | **PASS**, https://github.com/fahadhossain0909/aitos/actions/runs/42645724430 |
| Windows native / frozen build / install / uninstall | **PASS**, https://github.com/fahadhossain0909/aitos/actions/runs/42645729146 |
| Real ClickHouse restart / continuous-write soak | **PASS**, https://github.com/fahadhossain0909/aitos/actions/runs/42645743478 |
| Real Redis recovery | **PASS**, https://github.com/fahadhossain0909/aitos/actions/runs/42645738511 |
| Embedded ledger | **PASS**, https://github.com/fahadhossain0909/aitos/actions/runs/42645733798 |
| Operator gateway / built Chromium | **PASS**, https://github.com/fahadhossain0909/aitos/actions/runs/42645719550 |
| Frontend | **PASS**, https://github.com/fahadhossain0909/aitos/actions/runs/42645714869 |

PostgreSQL job completed in 1m10s, including actual disposable PostgreSQL 16 and
ClickHouse 24.12 setup and teardown. Both test steps passed; JUnit upload passed.
[JUnit artifact](https://github.com/fahadhossain0909/aitos/actions/runs/42645638096/artifacts/13686389221),
archive digest `sha256:a18c80fe30f5c7bb6a3cf9e6b3e0347eccf6b6601be0cb6c4709e8e8d1a6a4b6`.
Only this safe workflow was augmented with JUnit artifact preservation; no VPS
workflow or production settings were changed. No paper-canary/public-provider
workflow, signing workflow or main-target PR was triggered.

The corrected one-hour continuous-write run on **pre-integration** source
`6564415` also completed PASS:
https://github.com/fahadhossain0909/aitos/actions/runs/42511298412 .
This is historical evidence, not an hour-long run of the new integrated SHA.

### Integrated Windows candidate

The selected Windows native/build/install/uninstall job passed on the integrated
source. [Unsigned candidate artifact](https://github.com/fahadhossain0909/aitos/actions/runs/42645729146/artifacts/13687821261):
`UNSIGNED-Windows-console-test-only`, 16011850 bytes, archive digest
`sha256:3ebf5cf62138b70e25b947f02ad6e59c57316b00ff09009d98a5d0e70a04510f`.
This is not a trusted signed release or physical Windows GUI acceptance.
All eight selected workflows passed; none is pending at this final observation.
Raw artifact payloads were not downloaded through the restricted sandbox;
metadata/job outcomes are observed through GitHub API and artifact links are retained.

## Safety and remaining gates

Rechecked push filters: only the seven existing isolated workflows match this
exact branch. New upstream PostgreSQL and paper-canary workflows remain PR/manual;
only the disposable PostgreSQL one was explicitly dispatched. Existing VPS risk
still prevents opening a PR without resolving enable-state uncertainty.

This is author integration review, **NOT independent approval**. Prior reviewer,
trusted signing, real provider/account verification, actual Windows GUI acceptance,
HA/power-loss/capacity and recovery-authority gates remain. New code invalidates
any assumption that approval of an earlier SHA would approve this integration.
No new signature, real-account equality or production readiness is asserted.

## Rollback / handoff

All original branch and upstream history are retained in the merge parents.
No force/reset/clean or user-data deletion occurred. Do not reset the branch to
roll back: prepare a reviewed inverse patch/revert with the correct merge parent
if needed. No deployed runtime was changed, so no live service rollback is needed.
Keep branch-only features default-off/read-only and retain legacy financial
recovery authority until separately approved. Temporary test installations and
service containers are owned and cleaned up by their isolated CI jobs.
