# AITOS Root-Cause Register

## Register rules
- One row per distinct incident or reproducible defect.
- Do not promote a hypothesis to root cause without causal evidence.
- Link evidence, logs, traces, tests, commits and PRs where available.
- Keep resolved incidents with their verification evidence; do not erase history.

## Initial register
| ID | Severity | Symptom | Root cause | Confidence | Status | Evidence / next action |
|---|---|---|---|---|---|---|
| AITOS-ENG-001 | — | Engineering operating model and durable docs were not present at repository root | Documentation gap; no runtime defect implied | VERIFIED (repository inspection) | COMPLETE IN MAIN | `engineering/AGENT_OPERATING_MODEL.md` and `skills/aitos-skill.md` are present in main and define the agent workflow and repository-specific safety contract. |
| AITOS-ENG-002 | — | Runtime/latency baseline not yet captured by this work | Environment/runtime not accessed in this documentation change | VERIFIED (scope limitation) | NOT MEASURED | Run isolated paper-runtime audit and populate baseline |
| AITOS-ENG-003 | — | Open testnet integration PRs may overlap | PRs #210 and #213 are closed; PR #215 remains open and has overlapping Binance Futures Testnet scope | VERIFIED (GitHub PR search) | OPEN — REVIEW PR #215 SEPARATELY | Do not merge the testnet PR into the P-0/P-1/P-2 remediation set without a dedicated diff/CI/runtime review. |
| AITOS-ENG-004 | P2 | Data-flow latency exporter may publish 0 for stages with no latency observations | `_append_data_flow_metrics()` previously converted missing p50/p95/p99 values to numeric zero | VERIFIED (source and PR #217 merged) | RESOLVED IN MAIN — RUNTIME NOT MEASURED | PR #217 was merged. Unobserved percentile series are omitted, `aitos_data_flow_latency_sample_count` is exported, and genuine 0.0 ms values remain distinguishable from missing samples. Runtime scrape validation remains part of the combined runtime pass. |
| AITOS-ENG-005 | P2 | Startup failure during paper-runner initialization may bypass graceful cleanup | The main runtime `try/finally` began after Redis, ClickHouse, system modules, recovery, and auxiliary agents had already been initialized | VERIFIED (control-flow inspection) | FIX IMPLEMENTED — NOT VERIFIED | `run_paper_trading.py`: wrap startup in `AsyncExitStack`, register cleanup immediately after each resource is acquired, disarm the startup stack only after all startup wiring succeeds, and retain the existing runtime `finally` as the normal teardown owner. `TradeStatePersistence.initialize()` now retains each successful subscription incrementally so partial initialization can be rolled back. Added failure-injection tests for ClickHouse startup failure and partial subscription registration. CI/runtime pending. |
| AITOS-ENG-006 | P2 | Self-heal coverage differed from health-report coverage | HealthServer received core modules plus recorder, MarketOS persistence and four agents; periodic self-heal loop iterated only `components.all_modules()` | VERIFIED (source) | FIX IMPLEMENTED — NOT VERIFIED | Added `restart_unhealthy_modules()` for explicitly owned auxiliary modules; paper self-heal now includes MarketOS persistence, LearningExperienceRecorder and the four AI agents. Their shutdown paths now reset initialized state; recorder and MarketOS subscriptions are registered incrementally and rolled back on partial failure; MarketOS health now reports a dead flush task as unhealthy. Added restart-order and module reinitialization tests. CI pending; runtime health recovery remains deferred. |
| AITOS-ENG-007 | P2 | HTTP-only health probes may miss degraded module state | `/health` intentionally returns HTTP 200 when any module is DEGRADED; payload reports `status=degraded` | VERIFIED (source; intentional behavior) | DOCUMENTED | Consumers must inspect JSON status/module entries, not HTTP status alone. Keep behavior unless CD/readiness contract is explicitly changed. |
| AITOS-ENG-008 | P2 | Position-lifecycle API is not durable history | `_RECENT_EVENTS` is a process-local deque capped at 2,000; restart clears it and old events roll off | VERIFIED (source; documented limitation) | DOCUMENTED | Use durable structured logs/journal for historical forensics; do not treat absent event as proof no HOLD/exit occurred. |
| AITOS-ENG-010 | P0 | Trading startup could continue after restored positions failed market-feed resubscription | Paper runner ignored the resubscription verification result; live runner restored positions but did not perform the explicit post-initialization resubscription/verification step | VERIFIED (source control-flow inspection) | FIX IMPLEMENTED — NOT VERIFIED | `run_paper_trading.py`, `run_live_trading.py`, `TradeStatePersistence.resubscribe_restored_positions()`. Added a shared fail-closed readiness gate: if any restored position exists, every ingestion instance must verify both trade and kline symbol coverage; otherwise the scan loop is skipped and managed shutdown runs. Regression tests cover empty portfolio, missing feeds, partial verification, failures and malformed evidence. GitHub CI pending; runtime verification intentionally deferred. |
| AITOS-ENG-009 | P2 | Data-flow registry retains per-symbol buckets for the process lifetime | `_buckets` is keyed by `(stage, symbol)` and has no eviction/TTL; memory impact remains unmeasured | VERIFIED (source); memory impact is still a HYPOTHESIS pending runtime churn/RSS measurement | INSTRUMENTED — RUNTIME BASELINE PENDING | Added `cardinality_snapshot()` plus Prometheus gauges for total buckets, symbol buckets, aggregate buckets, unique symbols and per-stage bucket counts. No eviction is introduced before runtime evidence; the later combined runtime pass must capture bucket count and process RSS during dynamic-universe rotation and then decide whether bounded LRU/TTL is justified. |
 
## Incident template
### AITOS-YYYY-NNN — short title
- Severity:
- First observed (UTC):
- Last observed (UTC):
- Mode/environment:
- Affected components:
- Status: INVESTIGATING / MITIGATED / FIX IMPLEMENTED — NOT VERIFIED / RESOLVED / BLOCKED
- Symptom:
- Evidence:
- Reproduction:
- First divergence:
- Confirmed root cause:
- Competing hypotheses:
- Downstream effects:
- Remediation:
- Approval required:
- Tests and results:
- Runtime verification:
- Rollback:
- Remaining risks:
- Next action:

## 2026-11-07 — Current-main audit additions

Evidence, reproduction commands and rollback for these entries:
[`BACKEND_AUDIT_2026-11-07.md`](BACKEND_AUDIT_2026-11-07.md).

| ID | Severity | Finding | Confidence | Remediation / remaining gate |
|---|---|---|---|---|
| AUD-20261107-01 | P1 research | Development backtest mutated the strategy later copied into OOS windows | ROOT CAUSE CONFIRMED | Development strategy deepcopy; regression-verified; arbitrary closures/external state not certified |
| AUD-20261107-02 | P1 research | Shallow experiment event copies contaminated comparisons and caller history | ROOT CAUSE CONFIRMED | Per-cell deepcopy; nested mutation regression; large replay resource cost NOT MEASURED |
| AUD-20261107-03 | P2 | Different sample policies shared IDs; duplicate cost names ambiguous | ROOT CAUSE CONFIRMED | Policy in ID, duplicate-name rejection; complete provenance remains pending |
| AUD-20261107-04 | P2 testing | Two dependency distributions overwrite `aioresponses` import files | ROOT CAUSE CONFIRMED | Align backtest profile with maintained fork; clean reinstall + focused tests verified |
| AUD-20261107-05 | P2 testing | Non-integration selection still includes real-ClickHouse wiring tests | REPRODUCED | Database absent; classify fixtures/markers and obtain service-backed evidence; not hidden or bypassed |

### 2026-11-07 — Resource ownership continuation

| ID | Severity | Finding | Confidence | Remediation / remaining gate |
|---|---|---|---|---|
| AUD-20261107-06 | P1 reliability | Failed/cancelled repository initialization leaked clients across retries | ROOT CAUSE CONFIRMED | Cleanup on failed setup in market/journal/decision stores; preserve original failure and failed-close handle; real transport NOT TESTED |
| AUD-20261107-07 | P1 reliability | Shutdown left readiness true and client reference stale; initialize returned without reconnecting | ROOT CAUSE CONFIRMED | Disable writes before close; reset on success; explicit retryable cleanup; real-service restart NOT TESTED |
| AUD-20261107-08 | P2 reliability | Partial app subscription failure lost prior handles; cost route registered twice | ROOT CAUSE CONFIRMED | Incremental ownership, unique route; no claim of duplicate financial effects from redundant group consumers |

Details, tests, scope and rollback: `BACKEND_LIFECYCLE_AUDIT_2026-11-07.md`.
AUD-20261107-05 classification follow-up: the seven real-ClickHouse app-wiring tests now
carry the existing `integration` marker. They are not skipped by full CI; actual database
verification remains BLOCKED locally. Unit route ownership tests are separately service-free.

### 2026-11-07 — Expanded recovery/quality package

Detailed evidence, commands, limits and rollback: `BACKEND_RECOVERY_QUALITY_2026-11-07.md`.

| ID | Severity | Finding | Confidence | Status / next gate |
|---|---|---|---|---|
| AUD-20261107-09 | P2 | Failed/cancelled Redis PING lost owned attempt client | ROOT CAUSE CONFIRMED | Cleanup attempts implemented; mocked failure and success-transfer tests pass |
| AUD-20261107-10 | P2 | Cancelled Neo4j startup bypassed driver close | ROOT CAUSE CONFIRMED | Cleanup/cancellation propagation implemented; real Neo4j NOT TESTED |
| AUD-20261107-11 | P1 | ClickHouse helper missed non-retry-exhaustion failures and sibling cleanup | ROOT CAUSE CONFIRMED | Both cleanup attempts and original failure preservation tested; actual DB NOT TESTED |
| AUD-20261107-12 | P2 | Constructor coroutine leak, missing watchdog shutdown/restart ownership | ROOT CAUSE CONFIRMED | Lifecycle fixes; synchronous and decorator-order regressions pass; warning-free isolated suite |
| AUD-20261107-13 | P1 | Failed group creation/reset cached as success | ROOT CAUSE CONFIRMED | Cache only confirmed success; retry regression passes |
| AUD-20261107-14 | P1 | Idempotent executor inherited unsupported status stub instead of forwarding lookup | ROOT CAUSE CONFIRMED | Explicit read-only delegation and exception propagation tested |
| AUD-20261107-15 | P2 | Learner model/lock startup failures leaked client or masked cause on cleanup failure | ROOT CAUSE CONFIRMED | Broadened startup ownership; four-stage failure matrix passes |
| AUD-20261107-16 | P2 | One-shot learning batch failure skipped close | ROOT CAUSE CONFIRMED | Finally cleanup; success/failure CLI tests pass |
| AUD-20261107-17 | P0 | Mock Binance NEW/zero fill is reported as requested full fill | REPRODUCED | OPEN — no live activation; coordinated durable ACK/pending/fill reconciliation contract required, not a zero-fill/reject shortcut |

Redis evidence update: six real isolated Redis tests passed including crash/AOF/PEL
reclaim. This is not real account/position recovery certification or HA/power-loss proof.


### 2026-11-07 — Execution and numerical integrity

Evidence, reproductions and rollback: `EXECUTION_QUANT_INTEGRITY_2026-11-07.md`.

| ID | Severity | Finding | Confidence | Status / next gate |
|---|---|---|---|---|
| AUD-20261107-18 | P1 | Caller cancellation killed shared submission and first-caller cache ownership was lost | ROOT CAUSE CONFIRMED | Task-owned settlement/shielding and orphan failure retrieval verified locally; enclosing lifecycle/capital cancellation remains open |
| AUD-20261107-19 | P1 | Client ID accepted conflicting requests; fallback rounded economics/omitted reduce-only; entry lacked logical identity | ROOT CAUSE CONFIRMED | Payload binding, exact normalized float key, stable opportunity entry ID; no durable/exactly-once guarantee |
| AUD-20261107-20 | P1 | Nonfinite thresholds/evidence and bool/fraction counts could bypass research gates | ROOT CAUSE CONFIRMED | Shared metric/policy integrity checks pass; no statistical/profitability certification |
| AUD-20261107-21 | P1 | Invalid direct walk-forward durations could prevent cursor advancement | ROOT CAUSE CONFIRMED | Positive integer durations and aware ordered boundaries/timestamps validated before loop/sort |
| AUD-20261107-22 | P1 | Malformed order economics reached identity registration/inner executor | ROOT CAUSE CONFIRMED | Finite-positive numeric guard before side effects; invalid attempts cannot poison valid identity |

AUD-20261107-17 remains OPEN. Shielding is not a lifecycle/capital reconciliation
solution: enclosing cancellation may release capital while venue task continues.
Durable intent/partial-fill/protection/restart coordination is the next required gate.


### 2026-11-07 — Execution recovery and current conditional API

Detailed reproductions/rollback: `EXECUTION_RECOVERY_IMPLEMENTATION_2026-11-07.md`.

| ID | Severity | Finding / remedy | Evidence / remaining gate |
|---|---|---|---|
| AUD-20261107-17 | P0 | ACK/zero fill fabricated a full position; replaced with terminal finite-fill contract and bounded reconciliation | FIX IMPLEMENTED, VERIFIED offline; no actual exchange certification |
| AUD-20261107-23 | P0 | Live close/partial path did local accounting without a venue reduction | ROOT CAUSE CONFIRMED, RED 7 failures; actual reduce-only execution/application tests pass |
| AUD-20261107-24 | P1 | In-memory identity had no pre-submit restart evidence | Durable scoped market intent claim, multiprocess/hard-exit tests; protective intent extension remains open |
| AUD-20261107-25 | P1 | Exceptions/TTL could make exposed capital spendable | Pinned live reservations, stable scoped owner under OS lock, actual exposure restore; no distributed fencing |
| AUD-20261107-26 | P1 | Caller cancellation abandoned lifecycle after shielding just venue task | Whole live transition task-owned; drain/cancellation/application tests pass |
| AUD-20261107-27 | P1 | Router could fail over after ambiguous live execution | Uncertainty propagated; origin-unbound live reductions blocked |
| AUD-20261107-28 | P1 | Conditional protection assumed historical Binance endpoint/schema | Current official algo API/child-fill contract implemented and tested offline; actual venue NOT TESTED |
| AUD-20261107-29 | P1 | Restore lacked full account quantity comparison | Pure snapshot gate + canonical startup/scan/application checks; actual account recovery NOT TESTED |
| AUD-20261107-30 | P1 | Async event publication was the critical state owner; live teardown was split | Write-through application and unified cleanup; actual ClickHouse revision/failure behavior NOT TESTED |
| AUD-20261107-31 | P1 | Cancel errors swallowed; stop replacement removed old protection first | Terminal verification/new-before-old replacement; durable pending IDs and entry gate; protective POST-to-ID crash window open |
| AUD-20261107-32 | P2 | Blocking cTrader waits and missing TLS identity verifier | Off-loop wait/heartbeat regression and compatible dependency; broker/Windows security certification NOT TESTED |
| AUD-20261107-33 | P1 | Signed URLs could appear in transport exception telemetry | Sanitized private transport failures and suppressed original context verified |
| AUD-20261107-34 | P1 | Concurrent price events consumed one TP checkpoint twice | Per-live-position serialization and durable checkpoint accounting verified |

Do not change AUD-17 to unconditional production RESOLVED on these tests alone.
Market journal/application gates are not a complete protective-order intent ledger
or transparent automated account recovery. Canonical live activation remains blocked
by the gates explicitly listed in the dated implementation report.

## 2026-11-07 — Protective preclaims and read-only operator console

See [current implementation and evidence](CONSOLE_AND_PROTECTION_2026-11-07.md).
VERIFIED: protective pre-submit journaling + durable application acknowledgement;
read-only authenticated console/history adapter; seven-page frontend and browser
smoke. Current backend regression: **1630 passed, 15 deselected, 70.60s**.
Frontend: **13 unit tests**, production build, npm audit **0 vulnerabilities**.
NOT MEASURED: real ClickHouse revisions/restart, open-algo inventory recovery,
production latency/soak and native installers. No live orders or deployment.

## 2026-11-07 — Quality intelligence and latest-state recovery

| ID | Severity | Supported finding / remedy | Evidence / remaining gate |
|---|---|---|---|
| AUD-20261107-35 | P1 | Open-state predicate preceded latest-state aggregation, allowing older open rows to outlive later terminal rows in query selection | Latest retained version selected first; closed latest rows excluded after decoding; query-shape/unit regressions pass; actual ClickHouse NOT MEASURED |
| AUD-20261107-36 | P1 | Corrupt/ambiguous restored records could be skipped, silently omitting financial state | Retained latest-payload ambiguity/identity/corruption now blocks recovery; strict revisions/merged-history conflicts remain open |
| AUD-20261107-37 | P2 | Array-only frontend response checks allowed malformed financial payloads; detail dialog could retain failed-snapshot data | Typed runtime boundary, freshness checks and failure clearing; HTTP-fault browser smoke VERIFIED |
| AUD-20261107-38 | P2 | Tuning profile default could label attached runtime mode as paper | Canonical runners explicitly declare console runtime mode; default unknown; offline contract tests VERIFIED |

Offline scoped algo comparison and explainable readiness observations implemented;
neither is an account-recovery certificate or production grade. See
`QUALITY_BRAIN_IMPLEMENTATION_2026-11-07.md` for exact evidence/limits.

## 2026-11-07 — Real SQL, realized evidence and quant analytics

| ID | Severity | Supported finding / remedy | Evidence / remaining gate |
|---|---|---|---|
| AUD-20261107-39 | P1 | Aggregate alias `payload_json` shadowed its input in adjacent `uniqExact`, producing nested aggregation | ROOT CAUSE CONFIRMED on actual embedded ClickHouse: Code 184 ILLEGAL_AGGREGATION; distinct `latest_payload` fixes runtime/closed queries; 6 actual-engine tests pass; deployment version/transport still NOT MEASURED |
| AUD-20261107-40 | P1 | Closed restore used arbitrary tied `argMax` and skipped corrupt records, allowing realized evidence to disappear | Latest retained payload uniqueness + identity/terminal/finite-PnL validation; fail-closed restore/history; retained conflict tests VERIFIED; strict revisions/merged-away history remain open |
| AUD-20261107-41 | P2 | Default JSON serialization could write NaN/Infinity financial state | Nonfinite runtime/closed JSON rejected before insert; closed PnL/cost contracts enforced; legacy-data remediation remains separate |
| AUD-20261107-42 | P2 | Bounded UI output did not itself bound source-table read cost | Query-local time/bytes/memory guards with throw, not partial results; actual-engine one-byte-limit test raises TOO_MANY_BYTES |
| AUD-20261107-43 | P2 | Initial new analytics prefix could lose low-order PnL across large cancelling closes | Stable float expansion added before release; cancellation and grouped-timestamp regressions VERIFIED; no production incident claimed |

Research partition planning and the performance dashboard are read-only evidence
features, not a changed trading strategy or a profitable-edge certificate. Full
evidence: `QUANT_LEDGER_DESKTOP_IMPLEMENTATION_2026-11-07.md`.

## 2026-11-07 — Operator access and browser-verification boundary

| ID | Severity | Supported finding / remedy | Evidence / remaining gate |
|---|---|---|---|
| AUD-20261107-44 | P1 | A development proxy with a server-side runtime bearer could effectively authorize any visitor without a separate operator session | ROOT CAUSE CONFIRMED by prior proxy wiring; Vite now refuses the bearer and strips browser credentials; built gateway requires hash-only bounded sessions and strict read allowlists; 52 isolated gateway tests and real built-browser sign-in/revocation/expiry flow VERIFIED; nonlocal deployment/independent security review NOT TESTED |
| AUD-20261107-45 | P2 | Bundled headless browser flags disabled web security and origin-isolation features, making them unsuitable defaults for authentication regression evidence | Package web-security, insecure-content, isolation-trial and feature-disable flags removed from both harnesses; built authentication and nine-page smoke VERIFIED; CI headless process/sandbox flags are not native browser-security certification |

No exposed real account, compromised credential or production incident is claimed.
Implementation and limits: `OPERATOR_GATEWAY_IMPLEMENTATION_2026-11-07.md`.

## 2026-11-06 — Revision and ownership hazards (unresolved)

| ID | Severity | Supported finding / remedy | Evidence / remaining gate |
|---|---|---|---|
| AUD-20261106-46 | P1 | Replacement merge can erase tied conflicting payloads; retained-payload uniqueness then accepts a survivor | ROOT CAUSE CONFIRMED on temporary embedded runtime and closed-ledger tables, before/after forced FINAL merge; not fixed; append-only authority/projection design awaits approval |
| AUD-20261106-47 | P1 | Wall-clock milliseconds are not causal revisions or writer fencing | REPRODUCED older quantity after rollback and stale ahead-clock writer; distinct sub-ms updates share a version; no current writer topology/production clock incident claimed; transactional CAS/generation prototype proposed |
| AUD-20261106-48 | P1 conditional | Runtime/ledger identity and closed exclusion are unscoped for reused trade IDs | ROOT CAUSE CONFIRMED under controlled reused-ID account scenarios; existing global UUIDs do not demonstrate an actual collision; multiaccount/shared-data rollout must establish trusted scope |
| AUD-20261106-49 | P2 | Timestamp-selected income cursor is not a monotonic high-water mark | REPRODUCED cursor 200 -> 100 with newer timestamp; downstream replay/dedup impact NOT TESTED; no income-loss claim |

All four remain unresolved production gates. Eight passing counterexample tests
are evidence of the hazards, not evidence that fixes exist. See
`PERSISTENCE_REVISION_GATE_2026-11-06.md` for alternatives, approval boundary,
regression matrix and non-destructive rollout/rollback requirements.

### Revision-gate prototype evidence update — 2026-11-06

For AUD-20261106-46/47/48, the approved **unwired** journal now provides atomic CAS,
scoped identity, persistent storage generations and retained revision events.
Separate-process races/crashes and append-only projection behavior are VERIFIED.
This does not resolve those findings in the active legacy persistence path and
is not venue-order fencing. AUD-20261106-49 income cursor behavior is unchanged.
No new production incident is claimed; see
`REVISION_JOURNAL_PROTOTYPE_2026-11-06.md` for exact evidence and remaining gates.

## 2026-11-06 — Snapshot timestamp omission

| ID | Severity | Supported finding / remedy | Evidence / remaining gate |
|---|---|---|---|
| AUD-20261106-50 | P1 | Trade.to_dict omitted updated_at, so event reconstruction could fall back to entry time and collapse distinct update versions | ROOT CAUSE CONFIRMED: pre-fix KeyError reproduction; additive field fix; actual TradeStatePersistence event round-trip regression preserves two distinct timestamp/quantity updates; full backend 1856 passed. No production incident claimed; no historical backfill performed; timestamp versions still do not provide causal CAS/fencing |

New typed paper recovery protects only the isolated prototype. Prior replacement-
merge, stale-writer, scoped-identity and cursor findings are not thereby resolved
in the active legacy path. See `PAPER_RECOVERY_COMPLETION_2026-11-06.md`.

### Financial amendment prototype review — 2026-11-06

The previously absent immutable paper amendment contract is implemented and tested;
no new production incident/finding is asserted. Author review required validating
both the original parent and derived financial snapshot, plus scoped receipt/hash
uniqueness across trades. Corruption regressions now reject invalid-parent washing,
unauthorized gross-PnL rewrites and duplicate authoritative receipts even when row
hashes are recomputed. This is not independent review or a tamper-proof guarantee
against a privileged DB owner. Legacy active-path findings remain unchanged.
See `FINANCIAL_AMENDMENTS_2026-11-06.md` for exact limits and remaining gates.

## 2026-11-06 — Projector prototype binding blocker

| ID | Severity | Supported finding / remedy | Evidence / scope |
|---|---|---|---|
| AUD-20261106-51 | P2, prototype only | Nested Array(Tuple(...)) lookup parameters serialized incompatibly with the embedded query parser; replace with bounded scalar typed parameters, never interpolate data values | REPRODUCED / ROOT CAUSE CONFIRMED: three embedded failures, direct preflight Code 27 expected '('; actual source SELECT/insert/conflict/reopen now 3 passed. Not a production incident; no production migration |

All active legacy replacement-merge, ownership and reconciliation findings remain
open unless separately resolved by their own evidence. The explicit projector is
not a venue-execution fence, remote exactly-once guarantee or historical sink
certificate. A narrow reviewed Bandit B608 suppression covers generated numeric
placeholder structure; typed data binding and real embedded tests remain in place.
See `PROJECTOR_AND_PERSISTENCE_CONSOLE_2026-11-06.md` for remaining risks/rollback.

## Author review notes — paper/native tranche (2026-11-06)

No new production incident is asserted. Source review identified cancellation of
to_thread does not stop SQLite work; the optional mirror now shields/drains the
actual task and shutdown waits on its lock. A deterministic cancellation regression
verifies durable pending state and no premature shutdown. Harness invariants now
use explicit checks, not asserts removed by optimized Python. Native startup-close
race handling is implemented but Windows execution remains NOT TESTED. These are
author changes/review, NOT independent approval. Active legacy-path findings remain
open; the new runner mirror is shadow-only. Full evidence/rollback:
`WINDOWS_PAPER_INTEGRATION_2026-11-06.md`.

## Shadow snapshot / cancellation reproductions — 2026-11-06

| ID | Severity | Supported cause/remedy | Evidence/scope |
|---|---|---|---|
| AUD-20261106-52 | P2 prototype blocker | Caller mutated queued/threaded Trade after event identity capture; detach complete nested snapshot before lock/thread and persist that same object | ROOT CAUSE CONFIRMED: two pre-fix failing tests retained quantity 2 instead of 1; now pass. Default-off mirror, no production incident claimed |
| AUD-20261106-53 | P2 prototype blocker | Second cancellation cancelled native IO Future and released stop barrier early; repeated shielded drain, explicit terminal cancellation handling | ROOT CAUSE CONFIRMED: pre-fix double-cancel regression failed; now passes, terminal futures do not spin. Globally cancelled native outcome remains unknown/requires restart inspection |

Author review only, not independent approval. See
`PAPER_NATIVE_HARDENING_2026-11-06.md` for exact logs, scope and rollback.

## Recorded account boundary — 2026-11-06

No production incident claimed. Provider schema needs receipt-window/position-mode binding and margin-specific signed balances; implemented separately as schema 2, without reinterpreting schema 1 or modifying active financial authority. Strict loader rejects duplicate JSON fields/nonfinite constants. Existing active income cursor already holds unattributed rows in current source; no new cursor fix claimed. See `BINANCE_ACCOUNT_EVIDENCE_2026-11-06.md`.
