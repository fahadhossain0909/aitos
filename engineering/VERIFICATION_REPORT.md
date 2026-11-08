# AITOS Engineering Verification Report

## Scope
Engineering operating-model documentation and static source audit. The telemetry exporter correction and regression tests are isolated in PR #217. Runtime behavior remains NOT TESTED.

## Change verification
- Branch: `feat/engineering-agent-operating-system`
- Base: `main` at `b9d4a60a5ac1989455837fec277e2977d520de5e`
- Runtime application started: NO
- Exchange connected: NOT TESTED
- Redis / ClickHouse runtime: NOT TESTED
- Paper trade executed: NO
- Live order submitted: NO
- Latency measured: NO — NOT MEASURED
- Trading logic modified: NO
- Risk settings modified: NO
- Redis CPU/pool/concurrency modified: NO
- VPS workflows changed or resumed: NO

## Pull request
- Draft PR: https://github.com/fahadhossain0909/aitos/pull/216
- PR #216 current head before this report update: `94dd1f7ece1ffc9119c85d8c8cad1c94181ee211` (10 documentation-only files).
- Related PR #217 code head: `c1730d7730c42037bc804fe056f8c782e15744d0` (6 telemetry source/test/dashboard files).
- Initial PR workflow lookup returned no runs; combined status returned no checks. CI status: PENDING / NOT OBSERVED.

## Verification matrix
| Check | Result | Evidence |
|---|---|---|
| Existing README inspected | PASS | GitHub main README fetch |
| Existing agent skill inspected | PASS | `skills/aitos-skill.md` fetched |
| Existing prompt inspected | PASS | `skills/aitos-prompt.md` fetched |
| Root AGENTS.md existed before change | NO | GitHub contents API returned 404 |
| Runtime health | NOT TESTED | No runtime environment accessed |
| Unit/integration tests | PASS in GitHub Actions | 984 passed on Python 3.10, 3.11 and 3.12 on PR #217; new latency semantics test passed in each matrix. No local Windows test run. |
| CI | PASS (GitHub Actions) | PR #217 CI, Market Data Contract Audit and Backtest Tests passed. PR #216 CI passed on an earlier docs-only head; subsequent changes to PR #216 were documentation-only. |
| Independent review | PENDING | No separate human reviewer has reviewed either PR. |

## Additional static-source verification and branch remediation
- `aitos/health_server.py` contains health, metrics, data-flow and position-lifecycle routes.
- `aitos/forensics/data_flow_registry.py` uses an in-process bounded latency deque (maxlen 2048) and returns `None` for percentiles with no samples.
- Remediated on the engineering branch: `_append_data_flow_metrics()` now omits unobserved percentile series, exposes a per-stage latency sample count, and preserves a genuine observed 0.0 ms. Added regression coverage and Grafana sample-count panel. Tests/CI/runtime have NOT yet been executed.
- `monitoring/prometheus.yml` configures 15-second scrapes for paper/live endpoints. Scrape execution was NOT TESTED.
- `run_paper_trading.py` startup/teardown order, `aitos/app.py::initialize_all()/shutdown_all()`, and position lifecycle event retention were inspected. Findings AITOS-ENG-005 through AITOS-ENG-008 are recorded; no runtime failure is claimed.
- Added a regression test for empty samples and a genuine observed 0.0 ms; execution remains NOT RUN.

## CI details
- PR #217: 984 tests passed on each of Python 3.10, 3.11 and 3.12; Market Data Contract Audit passed (74 focused market-data tests plus 984 full-suite tests); Backtest Tests passed; Black, Flake8, Bandit/security dependency scan, Docker/Compose validation and image build passed.
- Optional Codecov upload did not complete because the repository has no valid Codecov token; this step is configured non-blocking and did not fail the test jobs.
- PR #216 CI passed on earlier documentation-only head `9822073966abb4ca37acd7eb0c65746b982ce18f`; subsequent PR #216 changes only updated documentation.

## Limitations
CI validates code and test behavior in GitHub-hosted isolated containers. It does not verify the user's local Windows paper runtime, real Binance WebSocket connectivity, local Redis/ClickHouse state, live Prometheus scrape, real latency distributions, or open-position monitoring/recovery. It also does not install/run a 24/7 agent or launch specialist agents.

## Acceptance criteria for the next runtime phase
- Every critical module has source and runtime evidence.
- Health/readiness distinguishes liveness from data-plane progress.
- Exchange data freshness and Redis consumer lag are measured.
- Decision-to-outcome tracing covers order and position lifecycle.
- p50/p95/p99 values have sample counts, window, mode and environment.
- Paper positions recover and remain monitored after restart.
- No live order or VPS deployment is used for validation.
- Independent review and CI pass.

## 2026-11-07 — Current-main backend package

Baseline `040fff9c`, branch `arena/ed127d35-aitos`. New source/reproduction evidence,
commands/results and remaining limitations are recorded in
[`BACKEND_AUDIT_2026-11-07.md`](BACKEND_AUDIT_2026-11-07.md). This is not a rerun or
certification of historical PR #216/#217 claims above.

Initial new regressions: 5 observed failures before repairs. After repairs and additional
coverage: learning/dependency tests 39 passed; adapter/executor/preparation mock tests
47 passed. Changed-file Black and dependency consistency checks passed. Broad suite
rerun: 1323 passed, 7 failed, 2 deselected. All seven failures attempted the unavailable
local ClickHouse server; full suite is NOT green. Details are in the dated report.
Runtime application, exchange connectivity, paper soak, recovery and latency remain
NOT TESTED / NOT MEASURED. Independent review and CI on these changes remain NOT TESTED.
No deployment, live order, risk/strategy limit modification or database migration.

## 2026-11-07 — Lifecycle ownership follow-up

New repository failure-injection tests: 18 failed / 3 passed before fixes; composition-root
ownership tests: 2 failed before fixes. After changes: targeted 35 passed in 0.79s.
Broad run before correcting test classification: 1346 passed, 7 failed, 2 deselected;
all seven failures required absent ClickHouse. They now have explicit integration markers
and remain selected by normal full CI (no skip/xfail). The post-classification non-integration
run and static checks are recorded in `BACKEND_LIFECYCLE_AUDIT_2026-11-07.md`.

No real service or execution activated; cancellation/restart assertions are mocked client
contracts, NOT proof of actual ClickHouse/Redis restart durability. Independent review,
Python matrix, latency, soak and paper-stack recovery remain NOT TESTED / NOT MEASURED.

Final post-classification isolated run: **1346 passed, 9 deselected, 2 warnings in 63.73s**.
Nine service-backed cases were not executed in this selection. Watchdog coroutine warnings
remain open. Seven changed-file Black checks, `compileall`, `pip check` and `git diff --check`
passed. No full-suite/service-backed certification is implied by isolated-suite success.

## 2026-11-07 — Expanded backend recovery package

Source/evidence/limitations: `BACKEND_RECOVERY_QUALITY_2026-11-07.md`.

- RED: infrastructure/telemetry 16 failed/1 passed; group/status 5 failed;
  learning startup/CLI 8 failed/2 passed, all before respective fixes.
- GREEN: 32 new mock/unit tests passed in 0.82s with RuntimeWarning treated as error.
- Final isolated suite: 1378 passed, 15 integration cases deselected in 65.84s, no warnings.
- Separate real Redis suite: 6 passed in 1.03s. Local binary built from Redis 7.2.7,
  test-owned Unix socket/data/AOF, no TCP listener or existing databases. No process left.
- Black 25.12.0: 13 changed Python files pass; production Flake8 fatal selection: 0;
  Bandit medium/high scan of six changed libraries: pass; pip check/diff checks pass.
- New workflow YAML parses; GitHub execution NOT TESTED. Full Python version matrix,
  ClickHouse/Neo4j services, independent review, live/paper soak remain NOT TESTED.
- Production-path latency and profitability remain NOT MEASURED.

Important: offline NEW/zero-fill response probe reproduced an unresolved P0 live execution
contract issue. Read-only status forwarding does not fix this. Passing suites are NOT
permission or evidence to activate real trading. The dated report defines pending-order
reconciliation and fault-matrix requirements before any activation.

Expanded package final checks: repository-wide Black 25.12.0 passed (596 files unchanged),
changed production compileall passed, and repeat real Redis run passed 6 tests in 0.91s.


## 2026-11-07 — Execution intent and quant numerical integrity

Detailed scope/evidence/limitations: `EXECUTION_QUANT_INTEGRITY_2026-11-07.md`.

- RED: intent ownership 11 failed/1 passed; quant integrity 41 failed/3 passed;
  extra string/mixed-timezone regressions 5 failed/65 passed; invalid execution
  economics 16 failed. Dangerous zero-duration tests were timeout-bounded.
- GREEN: 99 new tests passed in 0.41s, RuntimeWarning treated as error.
- Final non-integration suite: **1477 passed, 15 deselected in 67.71s**, no warnings.
- Existing execution/lifecycle + learning regression selection: 155 passed in 1.40s.
- Separate real isolated Redis: **6 passed in 0.90s**; no remaining Redis process.
  Nine older service-backed cases remain NOT TESTED locally.
- Black 25.12.0 repository-wide: 599 files unchanged. Five production-file
  compileall/selected Flake8/Bandit medium-high scans, pip check and diff check PASS.
- Intermediate 1434 passed/15 deselected preceded additional test cases and is not
  the final count. Final log: `tmp/execution-quant-final.log`.

Local task ownership is not durable order/lifecycle/capital/protection ownership.
P0 Binance NEW/zero-fill full-fill bug and capital release during uncertain exposure
remain unresolved live-activation gates. No live/exchange/deployment action performed.
Remote CI, independent review, Python matrix, paper soak/account recovery remain
NOT TESTED; production latency remains NOT MEASURED.


## 2026-11-07 — Durable execution and protection evidence

Report: `EXECUTION_RECOVERY_IMPLEMENTATION_2026-11-07.md`; operator routine:
`../docs/EXECUTION_RECOVERY.md`.

- RED: confirmed execution 16 failures; capital lifetime 2 failures; actual live
  reduction 7 failures; duplicate checkpoint 1 failure; malformed snapshot extension
  3 failures/21 passes; current algo contract 9 failures/3 passes.
- GREEN: **130 new cases passed in 2.78s**, RuntimeWarning treated as error.
- Final current-tree isolated suite: **1607 passed, 15 deselected in 67.67s**,
  no warnings; `tmp/backend-execution-final-verified.log`.
- Separate real isolated Redis: **6 passed in 0.94s**, no remaining Redis process.
  Nine older service-backed cases remain NOT TESTED locally.
- Real local SQLite reopen, competing processes, hard process exit and OS ownership
  tests pass. Composed venue/ClickHouse responses are mocks, NOT real account/DB
  recovery certification. Current API source pinned to official SDK f75b4a52.
- Black 25.12.0: 611 files pass; changed production compileall and Flake8 F selection
  pass; Bandit medium/high on 19 libraries pass; pip/diff checks pass. A rare-branch
  undefined logger found during verification was repaired with a forced pin-failure
  regression before final completion.

Protective pre-submit journaling/open-algo inventory, strict ClickHouse revision
semantics/real service recovery, approved paper soak, Python/OS matrix, remote CI
and independent review remain NOT TESTED / incomplete as applicable. Production
latency/throughput/profitability NOT MEASURED. Unsupported brokers are blocked, not
finished integrations. No exchange call, real order, live activation or deployment.

## 2026-11-07 — Protective preclaims and read-only operator console

See [current implementation and evidence](CONSOLE_AND_PROTECTION_2026-11-07.md).
VERIFIED: protective pre-submit journaling + durable application acknowledgement;
read-only authenticated console/history adapter; seven-page frontend and browser
smoke. Current backend regression: **1630 passed, 15 deselected, 70.60s**.
Frontend: **13 unit tests**, production build, npm audit **0 vulnerabilities**.
NOT MEASURED: real ClickHouse revisions/restart, open-algo inventory recovery,
production latency/soak and native installers. No live orders or deployment.

## 2026-11-07 — Evidence-led quality workspace and recovery hardening

VERIFIED: latest-state-first recovery with retained-conflict/corruption blocking,
offline protective inventory audit, ten-category deterministic readiness engine,
eighth frontend page, typed response/freshness boundary and timed journal batches.
Current backend regression: **1673 passed, 15 deselected in 68.02s**. Frontend:
**28 passed**, build/audit passed, eight-page and network-failure browser smoke
passed. Remote CI, real ClickHouse revisions/recovery, exchange/account evidence,
paper soak, production latency, native/auth/vault and LLM integration NOT MEASURED
or not implemented as detailed in `QUALITY_BRAIN_IMPLEMENTATION_2026-11-07.md`.
No live orders, activation, deployment or risk semantics changed.

## 2026-11-07 — Research planner, closed-ledger performance and actual SQL checks

VERIFIED: bounded conservative walk-forward date planner with locked holdout;
closed-PnL analytics and ninth UI page; closed-restore integrity; actual aggregate
alias root cause fixed; query-local throwing budgets; cross-language fixtures.
Current backend: **1731 passed, 21 deselected, 72.64s**. Six embedded ClickHouse
integration cases separately **6 passed, 0.86s** (embedded version 26.9.2.1, not a
production server). Frontend: **51 passed**, production build, audit 0 findings,
nine-page/planner/performance/failure browser checks passed. Detailed evidence,
limits and rollback: `QUANT_LEDGER_DESKTOP_IMPLEMENTATION_2026-11-07.md`.
Strict revisions, real-server restart/transport, account reconciliation, paper
soak/production latency, gateway/vault/native delivery and evaluated LLM still
require their explicit release gates. No orders, deployment or risk changes.

## 2026-11-07 — Authenticated local-first operator gateway

VERIFIED: single-operator hash-only, absolute-expiry sessions; protected built
console sign-in/logout/expiry clearing; four allowlisted read APIs with bounded
upstream I/O; strict Host/Origin/cookie/static boundaries. Vite now rejects a
runtime bearer and strips browser credentials. Development preview stays
unattached; production fails closed without a verified gateway session.

Current backend: **1783 passed, 22 deselected, 87.13s**, including 52 gateway
cases. Separately **6 embedded ClickHouse passed, 1.46s** and **1 built-gateway
Chromium authentication case passed, 14.33s**. Frontend **64 passed**; build,
audit (0 findings), nine-page browser and focused Python static checks passed.
Linux browser dependencies are reproducibly prepared from the locked npm package;
relaxed web-security/feature-disable package flags are removed. A no-deployment
operator-quality workflow is added; remote CI NOT TESTED.

See `OPERATOR_GATEWAY_IMPLEMENTATION_2026-11-07.md` and
`../docs/OPERATOR_GATEWAY.md` for evidence, exact commands, rollback and limits.
NOT TESTED: external TLS deployment, native OS delivery, independent penetration
review, real accounts, multiuser/MFA and vault. Strict persistence revisions,
real-service recovery/reconciliation, controlled paper soak and production latency
remain gated. No orders, live activation, deployment, migration, destructive
cleanup, strategy/risk changes, commit or push.

## 2026-11-06 — Persistence revision/fencing evidence and approval gate

REPRODUCED in temporary embedded ClickHouse: tied-payload replacement merges erase
conflict evidence; wall-clock rollback/stale ahead-clock writers can restore old
quantity; sub-millisecond updates collide; reused IDs are not isolated by account;
an unscoped terminal ID can suppress another scope; income cursors can regress.
The account/writer scenarios are synthetic conditions, not observed real topology
or UUID collisions. No production persistence fix is claimed.

VERIFIED: **14 embedded tests passed in 1.54s** (six existing safety cases plus
eight new passing hazard-characterization cases). Backend **1783 passed,
30 deselected in 55.71s**. Focused Black/F-lint/compile and whitespace checks pass.
Environment: Python 3.11.2, chdb 4.4.0, embedded ClickHouse 26.9.2.1. A no-migration
PR/manual workflow is defined; remote CI NOT TESTED. Frontend unchanged/not rerun.

Proposal and approval scope: `PERSISTENCE_REVISION_GATE_2026-11-06.md`. Next is an
unwired single-host paper SQLite transactional revision-journal prototype with
scoped identity, CAS, fenced generations, immutable events and an atomic outbox.
Explicit approval requested before this architecture-sensitive implementation;
no new production authority, migration, runner wiring or deployment authorized.
No orders, live activation, strategy/risk changes, non-test DB changes, cleanup,
commit or push. Production latency, real-server restart and account reconciliation
remain NOT TESTED/NOT MEASURED.

## 2026-11-06 — Approved unwired paper revision-journal prototype

VERIFIED after explicit founder approval: standalone SQLite WAL/FULL prototype,
trusted paper/backtest scope, generation ownership CAS, stale-writer rejection,
expected-revision CAS, immutable revision history, idempotent replay with current
head metadata, terminal protection and atomic event/head/outbox commits. No runner
imports/activates it. No existing journal, production persistence, event schema,
configuration, strategy/risk policy or non-test database changed.

Separate-process tests prove single-winner append and ownership races, fencing,
actual process-death rollback before commit and duplicate recovery after commit.
Test-only embedded MergeTree projection retains duplicates/conflicts after merge;
bounded evidence verification rejects inconsistent envelopes. No active projector
or cross-database exactly-once guarantee. Full Trade binding and amendments remain
out of scope. Storage fencing is NOT exchange-order fencing.

Final evidence: **1820 passed, 32 deselected, 62.91s** backend; **39 passed, 5.69s**
focused prototype/projection (overlapping); **16 passed, 1.66s** embedded suites
(overlapping). Focused Black/F-lint/Bandit, compile/whitespace, workflow syntax and
no-application-import checks pass. Remote CI/independent review NOT TESTED.
Production latency/capacity, venue/runtime health and power-loss resilience NOT
MEASURED/NOT TESTED; frontend unchanged/not rerun.

Details, limitations, rollback and next approval boundary:
`REVISION_JOURNAL_PROTOTYPE_2026-11-06.md`. Original legacy hazards remain unresolved
in the active persistence path. No orders, live activation, migration, deployment,
destructive cleanup, commit or push. Next: review production binding/monitoring,
execution ownership and legacy reconciliation before separately approved paper
wiring/restart/soak. Prototype approval does not approve that cutover.

## 2026-11-06 — Typed paper recovery and actual timestamp serialization fix

REPRODUCED/ROOT CAUSE CONFIRMED: Trade.to_dict omitted updated_at; persistence
reconstruction could fall back to entry time. The additive serializer fix and
actual persistence-event round-trip regressions preserve update timestamps.
VERIFIED: full-schema typed paper adapter, complete bounded scope recovery,
monitoring-failure/ownership/intent/feed rehearsal, and read-only legacy triage.
The real simulated lifecycle opens, restores into a new test lifecycle and closes
through SimulatedOrderExecutor. No real order or runner storage cutover occurred.

Final backend **1856 passed, 32 deselected, 67.93s**; **16 embedded passed, 1.33s**;
**64 frontend passed**; production build, audit 0 findings, nine-page browser and
**built auth browser 1 passed, 12.26s**. Focused static checks pass. Local synthetic
microbenchmark: 100 typed appends p50/p95/p99 2.142/2.668/3.411ms; 50 full recoveries
of 20 heads/100 historical events 10.213/10.598/10.966ms. Not production SLO evidence.

Exact contracts, logs, limitations, rollback and honest remaining-work ledger:
`PAPER_RECOVERY_COMPLETION_2026-11-06.md`. Prototype readiness never authorizes
entries/live; incomplete or merged legacy history is never declared importable.
No production migration/deployment, real-account access, risk/strategy/Redis
change, destructive cleanup, commit or push. Financial amendments, active worker/
runner wiring, real-service soak, independent review, native delivery and LLM
integration remain explicit unfinished gates, not silently declared complete.

## 2026-11-06 — Immutable paper fee/funding amendment contract

VERIFIED: explicit paper-only closed-trade amendment command with parent revision/
digest CAS, writer fencing, stable receipt/event idempotency, scoped receipt/hash
uniqueness, unchanged original close, exact allowed-delta replay and atomic event/
head/outbox commit. Generic append still rejects post-terminal mutation/reopening.
Typed recovery validates parent and new snapshot; duplicate receipt corruption
blocks scoped recovery/outbox. Existing simulated lifecycle calculation matches
canonical snapshots. Analytics uses latest heads; duplicate old/new close inputs
are rejected. No active runner, projector or exchange call was introduced.

Final backend **1888 passed, 33 deselected, 70.57s**; focused amendments **32 passed,
6.11s** (included); embedded **17 passed, 1.99s**; frontend **64 passed**; build,
audit 0 findings, focused Black/F-lint/Bandit/compile and whitespace checks pass.
Separate-process race and actual pre-/post-commit process exits verified. Existing
UI/auth browsers not rerun this increment. Remote CI/independent review NOT TESTED.

See `FINANCIAL_AMENDMENTS_2026-11-06.md` for receipt trust, float precision,
source/currency and replay limitations, exact commands and rollback. Prototype
amendment support is implemented, not production amendment/account certification.
Active projector/runner integration, real-service restart/soak, venue reconciliation,
independent review and native vault/installer remain open. No production DB change,
migration/deployment, live activation, risk/strategy change, cleanup, commit/push.

## 2026-11-06 — Projector and ten-page persistence console

VERIFIED locally: full non-integration backend **1917 passed, 36 deselected,
78.47s**; embedded persistence **20 passed, 1.71s**; built authenticated Chromium
**1 passed, 9.46s**; frontend **84 passed**, build PASS, ten-page browser smoke
PASS, npm audit **0 vulnerabilities**. Focused Black/F-lint/compile/whitespace
PASS; two workflow YAML files parsed; active-entrypoint prototype import guards
PASS. Focused Bandit exit 0 with one explicitly documented structural-SQL B608
suppression and a redundant-node warning; not independent security review.

Evidence includes real parameterized embedded lookup/insert/readback/conflicts,
actual spawned projector process exit after a durable test-sink receipt but before
ACK, reopen without reinsertion, and real isolated projector → API → gateway →
built browser. Last test uses synthetic inputs and an in-memory sink; process
crash uses a test-only fsynced JSON sink. Neither is a real remote-service soak.
Missing/malformed evidence clears UI counts/export; explicit examples stay
synthetic. See `PROJECTOR_AND_PERSISTENCE_CONSOLE_2026-11-06.md` and logs under
`tmp/projector-final/`, `tmp/console/projector-*`. No active worker or live control.
Remote CI, independent review, real-service restart/failover and native delivery
NOT TESTED; representative latency/capacity NOT MEASURED.

## Windows / paper integration tranche — 2026-11-06

VERIFIED locally: default-off paper runner shadow mirror/managed projector and
protected observer; legacy recovery/financial authority remains unchanged. No new
Redis consumer or tuning. Exact-decimal read-only account snapshot comparator/CLI,
Windows WinCred source, native manager, installer and separate unsigned/signing
workflows implemented. Service outage/restart/replacement-worker/soak harness
prepared; embedded SQL rehearsal passes, not real-service/HA verification.

Full backend **1980 passed, 37 deselected, 72.20s**; embedded **21 passed, 2.85s**;
frontend **84 passed**, build PASS; auth Chromium **1 passed, 10.07s**. Native
Windows test **1 skipped on Linux**. Focused static checks PASS. Real service run,
Windows build/GUI/install/signing and independent review NOT TESTED; no signed
release, journal authority cutover, provider account certification or latency SLO.
User selected Windows x64 / harness-first / reviewer-bundle-first. No activation,
production migration, orders, deployment, risk/strategy changes or commit/push.

Detailed contracts, commands, risks and rollback:
`WINDOWS_PAPER_INTEGRATION_2026-11-06.md`. Independent review remains pending;
`PAPER_NATIVE_REVIEW_BUNDLE.md` is preparation, not approval.

## Paper/native hardening — 2026-11-06

REPRODUCED / ROOT CAUSE CONFIRMED: mutable Trade snapshot changed before queued/
threaded persistence; repeated cancellation escaped the native-write drain barrier.
Detached snapshots now bind event identity/persistence before either boundary;
repeated cancellation drains safely, terminal cancelled futures fail closed.
Outer runner teardown always attempts shadow stop after legacy cleanup errors.

Shared native host now has actual loopback TCP/auth/revocation/thread tests. Both
Windows workflows use installed executable/network smoke and temporary uninstall;
signed validation verifies setup/app/uninstaller against the expected signer.
Service harness records stage/source/version/provenance. No real service run,
Windows/signing execution, provider reconciliation, cutover or independent approval.

Final local backend **2004 passed / 37 deselected / 72.10s**; embedded **21 passed /
2.88s**; frontend **84 passed**, build PASS; auth Chromium **1 passed / 13.70s**;
native Windows test **1 skipped** on Linux. Focused static PASS. No activation,
migration/deployment, orders, risk/strategy/Redis change or commit/push.
Details, evidence, remaining gates and rollback:
`PAPER_NATIVE_HARDENING_2026-11-06.md`. Earlier results remain historical.

## Recorded Binance account boundary — 2026-11-06

VERIFIED: offline Binance-shaped bundle adapter and schema-2 account comparison
bind provider/account/cut, position mode and receipt window. Exact signed margin
balances, hedge legs without netting, bounded receipt enumeration, duplicates and
terminal-page validation; strict bounded JSON/CLI. Schema 1 unchanged. No fetcher,
client, credentials, publisher/cursor changes or active authority integration.

Final backend **2045 passed / 37 deselected / 71.26s**; focused **76 passed**
(included), including **41 new cases**. Actual synthetic CLI exit-code 0/2/3
roundtrips PASS. Frontend **84 passed**, build PASS. Focused static checks and
workflow syntax PASS. Embedded/browser/Windows/service runs not repeated here.

No real account/provider authentication, production migration/cutover, orders,
service activation, deployment, risk/strategy/Redis change or commit/push. Structural
pagination is not proof of provider completeness. Details, limits and rollback:
`BINANCE_ACCOUNT_EVIDENCE_2026-11-06.md`. Independent review and external service/
Windows/signing/account verification remain pending.

## Actual hosted validation and publication

User authorized progressing the remaining gates. Source committed/pushed only to
`arena/ed127d35-aitos`: initial `bad5f03`, CI fix/tested source `15771eb`. Seven
no-deployment workflows now PASS: full backend/embedded, actual Windows vault/
build/install/auth-network/uninstall, real Redis recovery, real ClickHouse restart/
60-second soak, frontend, operator browser and ledger SQL. An additional actual
300-second ClickHouse restart/replacement-worker soak PASS (job 6m5s). Windows
**UNSIGNED test installer** artifact is available; not a trusted signed release.

Initial thin-env CI imports failed; fresh local repro showed missing aiohttp in
transitive imports. Install recipes fixed; retries passed without weakening tests.
Actual review request #247 created; no independent review asserted. No PR because
old PR-triggered VPS diagnostics may run and their enable flag is unreadable (403).
Signing environment absent from visible environment list; runner inventory blocked.
No signing dispatch, live/account access, production cutover/migration/deployment,
orders or risk/strategy/Redis tuning. Source validation is pinned to `15771eb`;
documentation-only handoff uses `[skip ci]`. Current evidence supersedes earlier
Windows/service NOT TESTED statements: `REMOTE_VALIDATION_EVIDENCE.md`.
