# AITOS Engineering Progress

## Current initiative
Establish a reusable, evidence-driven engineering operating model for any AI coding agent working on AITOS.

## Repository baseline
- Repository: `fahadhossain0909/aitos`
- Base branch: `main`
- Base commit inspected: `b9d4a60a5ac1989455837fec277e2977d520de5e`
- Work branch: `feat/engineering-agent-operating-system`
- Baseline date: 2026-11-03
- Runtime environment: NOT ACCESSED
- Trading mode: NOT VERIFIED
- Open overlapping testnet PRs observed: #210, #213, #215

## Pull request
- Draft PR: https://github.com/fahadhossain0909/aitos/pull/216
- Current PR #216 head: `94dd1f7ece1ffc9119c85d8c8cad1c94181ee211`
- PR #216 changed files: 10 (documentation-only).
- Related implementation PR #217 head: `c1730d7730c42037bc804fe056f8c782e15744d0` (6 code/test/dashboard files).
- GitHub Actions PR workflow runs observed at initial check: none; combined status checks: none. CI is PENDING, not passed.

## Progress
| Workstream | Status | Evidence / notes |
|---|---|---|
| Repository and open PR discovery | DONE | GitHub branch, commit and PR search |
| Existing agent instructions review | DONE | `skills/aitos-skill.md` and `skills/aitos-prompt.md` exist; root AGENTS.md absent |
| Root agent contract | DONE | Added on this branch |
| Engineering operating model | DONE | Added on this branch |
| Architecture/data-flow documents | DONE (initial map; runtime confirmation pending) | Initial logical map; source/runtime confirmation remains |
| Module inventory | PARTIAL | README and paper-runner/health source-level inventory; remaining modules not yet inspected |
| Telemetry specification | DONE | Specification and source audit documented; targeted code fix split into PR #217 for fix-branch policy |
| Latency baseline | NOT MEASURED | No runtime session or histogram export inspected |
| Root-cause register | PARTIAL | 9 source/documentation findings recorded; runtime root causes remain unverified |
| Runtime audit | BLOCKED / NOT STARTED | No connected local runtime or authorized isolated test environment in this session |
| Automated tests / CI | PASS (GitHub Actions) | PR #217: 984 tests pass on Python 3.10/3.11/3.12; market-data audit, backtest, formatting, lint, security and Docker/Compose checks pass. PR #216 CI passed on an earlier docs-only head; later changes were docs-only. Local runtime remains untested. |
| Independent review | NOT STARTED | Must review final diff and CI |
| Merge to main | NOT AUTHORIZED | Keep branch/PR isolated until explicit approval |

## Safety constraints carried forward
- Local-first testing. Do not deploy to VPS or resume paused VPS workflows.
- Do not change Redis CPU, pool, or concurrency tuning.
- Do not modify trading strategy semantics or risk parameters without approval.
- Never send live orders.
- Preserve main; do not force-push or force-merge.
- Require CI and appropriate runtime evidence before merge.

## Source-level audit findings
- Inspected `aitos/health_server.py`, core Event/Health contracts, data-flow registry/schema, Prometheus scrape config, data-flow tests, and Grafana dashboard query references.
- Confirmed AITOS-ENG-004 and moved its code remediation to dedicated `fix/telemetry-missing-sample` / PR #217 to comply with the operations skill's fix-branch rule. CI/runtime verification pending.
- Existing data-flow registry is process-local and retains a bounded 2,048 latency samples per stage/symbol bucket.
- Prometheus scrape interval is configured to 15 seconds for paper/live targets; actual scrape status remains NOT TESTED.
- Paper runner source audit found startup cleanup coverage begins only after a long initialization sequence (AITOS-ENG-005); auxiliary-module self-heal coverage differs from health-report coverage (AITOS-ENG-006); degraded health returns HTTP 200 intentionally (AITOS-ENG-007); position event API is bounded process-local memory (AITOS-ENG-008); per-symbol data-flow buckets have no source-level TTL/eviction (AITOS-ENG-009; memory impact NOT MEASURED).

## Next actions
1. Complete source inventory for exchange, Redis, ClickHouse, execution and risk modules.
2. Run the documented local paper-runtime audit on the user's safe local paper stack; inspect restored-position subscriptions and current open positions.
3. Capture real stage latency distributions and fill the baseline with sample counts/window/mode.
4. Verify decision-to-outcome lifecycle telemetry and Prometheus/Grafana rendering in the local runtime.
5. Investigate AITOS-ENG-005/006/009 with targeted failure-injection and symbol-churn tests before changing lifecycle or retention behavior.
6. Obtain independent human review for PR #216 and PR #217; do not merge without explicit approval.

## 2026-11-07 — Backend-first rebaseline and initial remediation

This dated entry supersedes old branch/PR status only for this new package; historical
evidence above is retained. Main baseline: `040fff9c`; session branch:
`arena/ed127d35-aitos`. PR #243/#245 merged; #244/#246 open at inspection.

- VERIFIED: fast-forwarded session branch after fetching complete shallow ancestry.
- ROOT CAUSE CONFIRMED: research strategy/event input contamination, ambiguous experiment
  identity/scenario names, conflicting HTTP mock dependency profiles.
- Implemented bounded offline fixes and regression tests; no trading-policy changes.
- Roadmap and desktop/control requirements: `BACKEND_FIRST_ROADMAP.md`.
- Exact tests, limitations, wiring gaps, rollback: `BACKEND_AUDIT_2026-11-07.md`.
- Service-backed runtime remains NOT TESTED; latency remains NOT MEASURED.
- No live order, deployment, Redis tuning, data deletion or main merge.

## 2026-11-07 — B1 resource and subscription ownership continuation

- ROOT CAUSE CONFIRMED: failed/cancelled ClickHouse schema initialization left clients
  owned but unclosed; retry could overwrite them. Three repositories now clean up failures.
- ROOT CAUSE CONFIRMED: repository shutdown retained ready state and blocked real restart.
  Shutdown now disables writes, closes/reset handles, and retains failed-close handles for retry.
- ROOT CAUSE CONFIRMED: list-expression subscription registration lost partial ownership;
  also registered the cost-adjustment route twice. Incremental handles and one route fix added.
- Initial RED evidence: 18 failed/3 passed repository cases; 2 failed route cases.
- Targeted GREEN evidence: 35 passed. Broader execution and limitations in
  `BACKEND_LIFECYCLE_AUDIT_2026-11-07.md`.
- Seven app-wiring tests require actual ClickHouse despite fake Redis/exchange. Added
  integration markers (not skip/xfail); full service-backed CI still selects them.
- No real database commands, trading activation, financial-policy changes or deployment.

Final isolated selection for this package: 1346 passed, 9 integration cases deselected,
2 existing watchdog warnings. Real-service certification remains BLOCKED locally, not DONE.

## 2026-11-07 — Expanded recovery/quality implementation

Evidence report: `BACKEND_RECOVERY_QUALITY_2026-11-07.md`.

- Implemented infrastructure retry cleanup, watchdog task ownership/restart,
  retryable group-success caching, read-only execution-status forwarding, and
  learning startup/one-shot cleanup. Trading/risk policies unchanged.
- New mock/unit regressions: 32 passed with RuntimeWarning promoted to error.
- Isolated real Redis 7.2.7: six delivery/ACK, crash/AOF/PEL reclaim, retry, DLQ,
  retry-write-failure and live-tail tests passed. Test-owned Unix sockets only.
- Final non-integration suite: 1378 passed, 15 deselected, no warnings. Six excluded
  Redis cases were separately exercised; nine older service-backed cases remain unverified.
- Added explicit-opt-in Redis recovery CI workflow; local YAML checks pass, GitHub run
  NOT TESTED. No VPS workflow resumed and no deployment performed.
- P0 execution ACK blocker reproduced offline: Binance NEW/zero executed quantity is
  reported as a full fill by current executor. Coordinated durable pending-order design
  and explicit execution-contract approval required; do not activate live trading.
- Production-path latency, actual position recovery and real ClickHouse/Neo4j integration
  remain NOT MEASURED / NOT TESTED. Desktop implementation remains deferred.


## 2026-11-07 — Execution intent and quant integrity package

Implemented on existing production paths: shared submission task ownership/shielding,
completion-owned cache/error retrieval, explicit ID-to-request binding, reduce-only
and full-float fallback identity, opportunity-based stable lifecycle entry ID, and
pre-submission finite-positive economics checks. Candidate/direct walk-forward gates
now reject nonfinite/malformed research metrics, noninteger counts/durations and
invalid temporal boundaries. No strategy/risk threshold changed.

Evidence and rollback: `EXECUTION_QUANT_INTEGRITY_2026-11-07.md`. 99 new tests pass;
final isolated suite 1477 passed / 15 deselected in 67.71s; separate real Redis rerun
6 passed in 0.90s. Black 599 files, changed-file compileall/fatal Flake8/Bandit,
pip/diff checks pass. Existing execution/lifecycle + learning selection 155 passed.

P0 Binance ACK/full-fill construction remains open. Shielding does NOT coordinate
lifecycle cancellation, capital retention, protective orders or durable restart;
the capital finally block can release a reservation while an inner task survives.
A coordinated durable pending/partial/uncertain-exposure bridge is required before
any live activation. No exchange call/deployment/commit/push. Frontend deferred.


## 2026-11-07 — Durable execution and protection continuation

Implemented on the canonical live path: pre-submit SQLite market intents; bounded
terminal fill confirmation/client-ID reconciliation; uncertainty quarantine and
no cross-venue retry; actual reduce-only close/partial execution; caller-independent
capital-to-protection task ownership; critical write-through state; stable scoped
pinned capital under OS runtime ownership; full-account snapshot checks at startup,
scan and application; graceful submission drain and unified teardown ownership.

Official current Binance SDK source (commit f75b4a52) revealed separate conditional
/algoOrder API. Implemented algo IDs/trigger/client fields and actual child-fill
reconciliation, retaining legacy numeric persisted IDs and explicit provider
compatibility. Live stop replacement keeps old protection until new acceptance and
durable ID write; pending old cancellations block new entries. Partial-close
telemetry and concurrent TP checkpoint consumption corrected. Unsupported broker
reductions are blocked; cTrader blocking waits moved off loop and TLS identity
dependency supplied; signed private transport URLs sanitized.

Final **1607 passed / 15 deselected in 67.67s**, no warnings; **130 new cases passed**;
separate real Redis 6 passed in 0.94s. Black 611 files and changed-file static checks
pass. Source, operational procedure and limitations:
`EXECUTION_RECOVERY_IMPLEMENTATION_2026-11-07.md`, `../docs/EXECUTION_RECOVERY.md`.

NOT complete/production-certified: protective pre-submit intent/open-algo crash
recovery, strict ClickHouse revision/real-service fault evidence, approved paper
soak, Python/OS/remote CI and latency gates. No real exchange call/order, deployment,
commit or push. Frontend deferred until these backend readiness gates are satisfied.

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

## 2026-11-06 — Explicit paper projector and persistence workspace

VERIFIED: bounded explicit `RevisionProjector.run_once`, atomic batch ACK,
pre/post sink evidence verification, ambiguous-response recovery, writer checks,
and optional authenticated/sanitized snapshot observation. No active service,
runner import, migration or live authority. Ten-page desktop now includes
Persistence & recovery with unavailable clearing, strict parser, explicit synthetic
illustration/provenance exports and responsive layout. Actual isolated projector
evidence reached the built authenticated UI through API/gateway; fixtures are
synthetic, not production certification.

Final local backend **1917 passed / 36 deselected**; embedded **20 passed**; built
auth browser **1 passed**; frontend **84 passed**, build/browser smoke PASS, npm
audit 0. Actual spawned-process pre-ACK death/reopen verified with a durable
test-only sink; actual sink adapter separately verified in embedded ClickHouse.
CI paths/tests extended; remote CI and independent review NOT TESTED. Focused
Bandit uses one documented structural-SQL B608 suppression, not a blanket exclusion.

See `PROJECTOR_AND_PERSISTENCE_CONSOLE_2026-11-06.md` for exact commands, limits,
rollback and remaining gates. This supersedes prior verification counts, not
historical results or unresolved active legacy-path findings. Next: independent
review/CI, separately approved runner/execution ownership and real-service soak;
account reconciliation and native vault/signed delivery remain unfinished. No
orders, activation, risk/strategy/Redis tuning, deployment, cleanup, commit/push.

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
