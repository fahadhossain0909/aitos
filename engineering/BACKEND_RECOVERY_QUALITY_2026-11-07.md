# Backend recovery and quality package — 2026-11-07

## Executive scope

Continuation of the backend-first roadmap. Implements infrastructure ownership,
telemetry task lifecycle, Redis group retry integrity, read-only execution-status
wiring and learning-worker startup cleanup. Adds real, isolated Redis crash/recovery
coverage and a dedicated CI definition. No trading activation, order submission to an
exchange, production deployment, threshold change, user-data deletion or migration.

Branch remains `arena/ed127d35-aitos`. Current persisted HEAD is `6f8052bb`; the prior
newer-main content and previous audit changes remain working-tree changes, as described
in the lifecycle report. They were preserved; this package did not reset/merge/rewrite
or push anything. Real-service evidence here is limited to test-owned Redis 7.2.7.

## Implemented findings

| ID | Severity | Root cause and change | Evidence |
|---|---|---|---|
| AUD-20261107-09 | P2 reliability | Failed Redis PING attempts lost client ownership before returning to the retry loop; close is now attempted on failure/cancellation, never on successful transfer to caller | ROOT CAUSE CONFIRMED; retry and cancellation tests |
| AUD-20261107-10 | P2 reliability | Neo4j cancellation bypassed cleanup; close now runs on failure/cancellation and cancellation propagates; ordinary optional-service failure remains `None` | ROOT CAUSE CONFIRMED; failure-injection tests |
| AUD-20261107-11 | P1 startup reliability | ClickHouse helper cleaned up only RetryExhaustedError and could abandon the sibling on close failure; now both repositories receive cleanup attempts on every startup exception/cancellation | ROOT CAUSE CONFIRMED; original cause/exit contract tests |
| AUD-20261107-12 | P2 observability | Safe watchdog coroutine created in constructor without a running loop; contention watchdog had no shutdown owner; both now start after successful initialization, stop/await on shutdown, and restart without duplicate tasks | ROOT CAUSE CONFIRMED; both decorator installation orders and failed start/stop tested |
| AUD-20261107-13 | P1 data transport | `_ensure_group` cached success in a finally block even when Redis creation/reset failed; cache now records only confirmed success/BUSYGROUP handling | ROOT CAUSE CONFIRMED; failed creation and live reset retry regressions |
| AUD-20261107-14 | P1 reconciliation wiring | Idempotent executor inherited the base `get_order_status` returning None; `__getattr__` could not delegate an existing method; explicit forwarding now preserves authoritative status and lookup failure | ROOT CAUSE CONFIRMED; no order-placement behavior changed |
| AUD-20261107-15 | P2 learning reliability | Learning constructor owned client/lock only around cursor load, not scorer/checkpoint/lock setup; all post-client setup now guarded, cleanup releases lock even if close raises, original startup failure preserved | ROOT CAUSE CONFIRMED; four startup stages with successful/failed close |
| AUD-20261107-16 | P2 learning reliability | `--once` skipped worker.close on batch failure; cleanup now in finally | ROOT CAUSE CONFIRMED; successful/failed CLI run tests |

Cleanup is best-effort, not a guarantee that a broken driver closed successfully.
Repeated external cancellation during cleanup can still interrupt it. This package does
not change retry counts, delays, Redis pool/concurrency, stream schemas or risk policies.
Learning singleton acquisition now owns only its lock; the constructor owns the client
cleanup, avoiding duplicate closes. Existing real multiprocess lock tests still pass.

## Real Redis verification

Docker and system redis-server were absent. Redis 7.2.7 source was downloaded from:
`https://codeload.github.com/redis/redis/tar.gz/refs/tags/7.2.7`.
Archive SHA-256: `fdbdee0a19a12e61c2eca46f4162f6d3ede3a01e0baa8a12cee1f35f3391f1e9`.
Built under ignored `tmp/redis-build`, with `MALLOC=libc`, TLS disabled, no vendored code
or compiled artifact tracked. This test version is not a production version recommendation.

`tests/integration/test_isolated_redis_recovery.py` accepts only an explicit local binary
path/name in `AITOS_TEST_REDIS_SERVER`. It starts a private process per test using a
pytest-owned directory and Unix socket (permissions 700), TCP disabled (`--port 0`),
AOF enabled and `appendfsync=always`. No external Redis URL or existing database accepted.
Processes are stopped in fixture teardown; no Redis process remained after the tests.

**6 passed in 1.03s**:
1. Real stream publish/consumer delivery and ACK, matching event ID/payload.
2. Hard Redis process kill after source/PEL writes, AOF restart, pending entry preserved,
   abandoned-consumer reclaim and ACK. Test overrides reclaim idle time to zero only
   inside the test; production setting is untouched.
3. Handler failure -> retry -> successful ACK of the same event identity.
4. Poison event -> bounded retries -> DLQ, with no source PEL entry left unacknowledged.
5. Injected retry-write failure leaves the real source PEL entry pending; successful
   replacement write then precedes ACK. Fault injection is at the client method boundary.
6. Live-tail subscription processes only fresh data, deliberately leaving historical
   pending entries untouched (documented existing mode semantics).

These tests do NOT establish power-loss durability, Redis HA/failover, real ClickHouse
state recovery, Binance connectivity, open-position survival or profitability. The AOF
result is for a single local Redis process under this test configuration, not all deployments.

Reproduce after supplying an approved Redis binary:

```sh
AITOS_TEST_REDIS_SERVER=/absolute/path/to/redis-server \
  .venv/bin/python -m pytest -q tests/integration/test_isolated_redis_recovery.py --timeout=30
```

New `.github/workflows/isolated-redis-recovery.yml` installs Redis in an ephemeral Ubuntu
runner and explicitly opts in to these six tests. YAML parsed locally; GitHub execution
is NOT TESTED (no push/PR/action dispatch in this package). No VPS workflows changed.

## Regression and static results

- Before fixes: infrastructure/telemetry tests **16 failed, 1 passed** in 0.84s.
- Before fixes: group retry/status delegation **5 failed** in 0.18s.
- Before fixes: learning startup/once cleanup **8 failed, 2 passed** in 0.57s.
- After fixes: **32 new mock/unit cases passed** in 0.82s with RuntimeWarning treated as
  an error; targeted existing suites also passed (38 infrastructure/eventbus and 22
  execution/switch tests; 47 learning tests in their respective invocations).
- Final non-integration suite: **1378 passed, 15 deselected in 65.84s**, no warnings.
  The 15 exclusions are six new real-Redis cases (executed separately above) plus nine
  previously classified service-backed cases. Do not call this a full-suite pass.
- Black 25.12.0 (same as current CI): **13 changed Python files pass**. Earlier Black
  26 formatting differed for persistent_state; it was corrected with the CI version.
- Flake8 repository production selection `E9,F63,F7,F82`: **0 findings**.
- Bandit `-ll` on six changed runtime/library files: **PASS** (no medium/high findings).
- `pip check`: **PASS**. `git diff --check`: **PASS**.
- Python 3.10/3.12 matrix, full CI, ClickHouse/Neo4j integration and independent review:
  NOT TESTED. Latency/throughput and market-data pipeline baseline: NOT MEASURED.

## Important unresolved execution blocker — AUD-20261107-17

Severity: P0 potential financial-state integrity. Status: REPRODUCED in an offline
mocked-response probe; NOT activated or observed against a real account.

`BinanceFuturesOrderExecutor.submit_order` accepts `NEW` with `executedQty=0`, substitutes
requested quantity when reported filled quantity is zero, and substitutes reference price
when exchange average price is zero. `TradeLifecycle` then treats successful results as
filled and emits `order.filled` / creates a position. The probe (no HTTP/session/network)
returned **success=True, filled_quantity=1.0** from **NEW, executedQty=0** for a 1-unit
request. Raw local evidence is in ignored `tmp/ack-contract-evidence.json`.

Do NOT activate live trading on the basis of passing tests in this package. A minimal
"return failure" or "return zero" change is unsafe: an accepted exchange order may still
exist and fill later, while local capital/protection ownership could be released or lost.
A coordinated order/ACK/pending/reconciliation contract is required before changing
financial behavior. It was not silently bundled into read-only status delegation.

### Proposed execution contract (approval/implementation gate)

1. Allocate a stable logical intent ID before submission, durable across restart. Economic
   parameter equality is not intent identity; identical legitimate later trades must not
   be permanently deduplicated. Include account/venue/mode ownership.
2. Persist a bounded order intent with requested quantity, symbol, side, reduce-only,
   capital reservation, client ID and submission state before any network side effect.
3. Distinguish ACK/NEW, partial fill, terminal full fill, terminal zero-fill, and unknown
   transport outcome. Only exchange-confirmed executed quantity enters the position ledger.
4. For timeout/cancellation/ambiguous HTTP failure, retain intent/reservation and reconcile
   by the same client ID. Never blind-resubmit, infer no order from local absence, or label
   an unsupported/unavailable status query as a confirmed rejection.
5. Monitor/protect actual partial exposure while reconciling the remainder. Reconcile open
   orders and actual exchange positions on restart before permitting new risk.
6. Process fills, partial exits, costs and terminal release idempotently. Unknown state must
   block unsafe new entries without disabling protection for existing exposure.
7. Add bounded retry/timeout policies, durable audit reasons, authenticated operator/manual
   resolution, and independent review. No automatic flattening or live activation.

Acceptance matrix: NEW/no-fill; partial then filled; partial then cancelled; accepted but
ACK lost; lookup timeout/not-found ambiguity; crash before/after submission and fill write;
duplicate delivery; concurrent same intent; separate intents with identical economics;
late fee/funding; reduce-only close; restart with orphan order or position; rollback.

Other idempotency audit concerns remain HYPOTHESIS pending dedicated tests: in-flight task
cancellation ownership, request-ID collisions, and durability of completed-order caches.
They are not certified by the status-delegation fix or by the mock Binance response probe.

## Files and rollback

Production: `aitos/runtime_connections.py`, two forensic telemetry modules,
`aitos/eventbus/redis_bus.py`, `aitos/trading/persistent_state.py` (read-only status method),
`aitos/learning/worker.py`, `run_continual_learning.py`.
Tests: five new unit/mock files plus `tests/integration/test_isolated_redis_recovery.py`.
CI: isolated Redis recovery workflow. Documentation: this report and append-only entries
in progress, root-cause register and verification report.

Revert only this package's hunks/new files if required; preserve prior changes and user
data. No schema/data migration rollback needed. Real Redis artifacts are ignored test
scratch, not application dependencies. Backend certification and desktop work remain
unfinished; the live ACK contract, real financial-state recovery, quant evidence and
measured production-path latency are still explicit gates.

### Final verification follow-up

Repository-wide Black 25.12.0 check: **PASS, 596 files unchanged** (not just changed files).
Compileall of the seven changed production/entrypoint files: PASS. Real Redis suite rerun:
**6 passed in 0.91s**, confirming repeatability after the final documentation/static pass.
