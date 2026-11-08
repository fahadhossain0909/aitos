# Durable execution and protection implementation — 2026-11-07

## Executive scope and readiness decision

Fifth backend-first implementation package, current branch `arena/ed127d35-aitos`,
HEAD `6f8052b`. Existing broad founder/prior changes remain preserved. No commit,
push, live activation, exchange call, remote deployment, existing-database mutation
or real order was performed. New journal DDL was exercised only in test-owned SQLite.

**The former NEW/zero-fill -> fabricated full fill path is replaced and VERIFIED in
offline fault tests.** This is substantially more than a parsing patch: canonical
market execution now has pre-submit durable intent, bounded status/cancel settlement,
capital retention, caller-independent lifecycle ownership, write-through application,
account quantity checks and actual reduce-only exits. Current Binance conditional
API compatibility was researched and implemented separately.

**The whole backend is not certified complete/production-ready.** Mandatory real
service/recovery/soak/latency gates and a pre-submit protective-intent extension remain
open below. Unsupported brokers are explicitly fail-closed, not falsely described as
completed integrations. Frontend remains deferred under the founder's backend-first
instruction.

## Research and actual call paths

Reread AGENTS.md, the reliability skill/operating model, README and prior execution
report. Traced `run_live_trading -> prepare_live_executor -> IdempotentOrderExecutor ->
TradeLifecycle`, installed capital/position decorators, close/partial/reconciliation,
state persistence, reservation lease expiry, routing and optional broker adapters.

Source inspection plus seven failing tests confirmed that the ordinary lifecycle
close/partial-close path previously performed local accounting without submitting an
actual venue reduction. Existing position-switch close adapters already performed
an independent reduction, so blindly adding a close POST everywhere would duplicate
switch exits; the integration now explicitly finalizes the confirmed switch receipt.

### Current official protocol reference

Read via GitHub API; no Binance account/network request was made:

- Official current SDK repository: `binance/binance-connector-python`, commit
  `f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179`.
- Pinned source:
  https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/api/trade_api.py
- It documents `POST /fapi/v1/order` with `newOrderRespType=RESULT` for actual market
  fills; separate `POST/GET/DELETE /fapi/v1/algoOrder` for conditional TP/SL orders,
  and `triggerPrice`, `clientAlgoId`, `algoType=CONDITIONAL`.
- Pinned new/query/cancel algo response models distinguish `algoId`, `algoStatus`
  and `actualOrderId`. Triggering is not equivalent to a child order being filled.
- Pinned trade_api.py SHA256:
  `0f482140acd6561cc02571110767e5c9dd363a671be0542a859536f014b1c603`.
- Initial historical connector reference was also inspected, but it is not the
  sole current API authority and no deprecated SDK was added to requirements.

GitHub SDK source supports the implemented wire contract, not a claim of successful
venue connectivity, current account entitlements or actual testnet operation.

## Findings and changes

| ID | Severity | Root cause / implemented response | Evidence |
|---|---|---|---|
| AUD-20261107-17 | P0 financial state | NEW/zero quantity and partial ACK were reported as full requested fill; reference price substituted for execution price | Terminal finite quantity/price evidence only; RESULT requested; client-ID lookup then bounded cancel/final lookup; cancelled partial fill remains actual exposure; unknown stays uncertain |
| AUD-20261107-23 | P0 execution | Generic lifecycle close/partial-close did local accounting without live reduction | Live capability explicitly requires venue execution; stable reduce-only exits; actual quantity/price accounting; partial full-exit result stays open; confirmed switch/resting fills not submitted twice |
| AUD-20261107-24 | P1 recovery | In-memory intent cache could not prove pre-submit/restart uncertainty | SQLite scoped claim committed before market POST, payload conflict rejection, cross-process claim serialization, CONFIRMED until durable application, fail-closed startup; offline read-only inventory CLI |
| AUD-20261107-25 | P1 capital | Cancellation/exceptions released a lease despite potential exposure; TTL could free submitted/open capital | Live submitted/open capital pinned until explicit release; stable scoped live owner under OS runtime lock; conservative restore after matching account snapshot; paper lease behavior preserved |
| AUD-20261107-26 | P1 ownership | Shielding only the venue task still allowed caller cancellation to abandon lifecycle/protection | Entire live capital/execution/protection/application transition task-owned and shielded; orphan exceptions retrieved; submission drain before removing critical callbacks |
| AUD-20261107-27 | P1 routing | Broad exception fallback could repeat an uncertain live submission on another venue | Uncertainty propagates; live failures cannot trigger cross-venue retry; origin-unbound routed live reductions fail before submission |
| AUD-20261107-28 | P1 API compatibility | Legacy stop endpoint/schema assumed to be the current Binance conditional contract | New algo endpoint/fields; persisted `algo:` namespace; linked child execution verification; terminal partial child reconciliation; classic numeric IDs remain compatible |
| AUD-20261107-29 | P1 recovery | Local restored positions were not matched to full account quantities | Read-only full-account position snapshot checks, hedge/one-way direction and signs, unknown/manual positions, duplicate/malformed rows; checks at startup, scan cycle and execution application |
| AUD-20261107-30 | P1 lifecycle | Persistence depended on asynchronous event publication; live teardown ownership split across paths | Critical write-through callback, archive-before-delete, intent acknowledgement after write/verification; one AsyncExitStack teardown owner, cleanup attempts survive sibling errors |
| AUD-20261107-31 | P1 protection | Cancellation errors swallowed as benign; live SL replacement cancelled protection before confirming replacement | Verified terminal cancellation/read-back, unknown/permission errors not swallowed; replacement first, durable new+old identities, pending old cancellations block entries |
| AUD-20261107-32 | P2 responsiveness/security | cTrader Event.wait blocked event loop; Twisted warned hostname identity verifier absent | Waits moved to to_thread without skipping work; deterministic heartbeat test; compatible service-identity dependency installed/declared |
| AUD-20261107-33 | P1 confidentiality | Transport exceptions may embed signed private request URLs in upstream telemetry | Sanitized private transport exception, suppress original URL context; structured API codes remain available |
| AUD-20261107-34 | P1 concurrency | Concurrent price events could both consume the same TP checkpoint | Per-live-position price serialization plus mutation locks; checkpoint consumption in durable partial application; partial exit telemetry no longer says completed full close |

ROOT CAUSE CONFIRMED for the executed RED cases and directly traced contracts above.
Other guard coverage is VERIFIED locally, not independent runtime certification.
No configured strategy entry/exit threshold, leverage or risk limit was changed.
Live notional/risk/PnL now describe confirmed actual exposure rather than requested
size; this is accounting correction, not a new sizing policy. Paper accounting and
lease defaults remain on their existing path.

## Implemented files and ownership

New production modules:
- `aitos/execution/intent_store.py`: single-host SQLite intent/quarantine state.
- `aitos/execution/position_integrity.py`: pure full-account quantity comparison.
- `aitos/execution/recovery.py`: existing-journal read-only inventory, no reset action.
- `aitos/runtime_ownership.py`: portable nonblocking OS ownership of a state root.

Extended existing executor interfaces/implementations, live preparation/runner,
capital gateway/reservations/runtime guard, lifecycle/reconciliation/switch handoff,
state persistence and existing position telemetry. No alternative scanner, risk
framework, strategy engine, frontend or disconnected execution subsystem was added.

Canonical factory always configures the market-intent store. Low-level executor
construction with `intent_store=None` retains compatibility for SDK-style/offline
callers and does not promise restart durability. The canonical runtime holds its
state-root ownership lock while using the stable live-capital owner; that owner
must not be shared by unlocked concurrent workers.

## Executed verification

### RED reproductions

- Confirmed market execution: **16 failed in 0.23s**,
  `tmp/confirmed-execution-red.log`.
- Capital lifetime/uncertainty: **2 failed in 0.16s**,
  `tmp/execution-capital-red.log`.
- Live reduction/application: **7 failed in 0.18s**,
  `tmp/live-reduction-red.log`.
- Concurrent TP checkpoint: **1 failed / 7 deselected in 0.14s**,
  `tmp/price-checkpoint-red.log`.
- Snapshot malformed/bool/missing data extension: **3 failed / 21 passed in 0.16s**,
  `tmp/position-snapshot-red.log`.
- Current algo contract: **9 failed / 3 passed in 0.18s**,
  `tmp/algo-protection-red.log`.

### GREEN and static evidence

- **130 new tests passed in 2.78s**, RuntimeWarning treated as error, across eight
  new files: confirmed execution, intent recovery, capital ownership, live reduction,
  composed live application, snapshot integrity, cross-boundary safety and algo API.
- These include real local SQLite reopen, competing OS processes, hard subprocess
  exit after a committed pre-submit claim, cross-process runtime exclusion and stable
  capital-owner restart/release. Venue responses and critical ClickHouse writes in
  the composition tests are OFFLINE mocks, not real account/DB evidence.
- Separate real isolated Redis rerun: **6 passed in 0.94s**, private Unix socket,
  test-owned AOF/data, no existing databases or TCP listener. No Redis process left.
- Intermediate non-integration runs: **1516 / 1587 / 1603 / 1604 passed**, each with
  15 integration cases deselected, while coverage was expanded. They are historical
  intermediate counts, not the final result for the full final tree.
- Final isolated regression result: recorded in the completion addendum below.
- Black 25.12.0: **611 repository Python files unchanged**.
- Changed production compileall: PASS; selected fatal/undefined-name Flake8 and full
  `F` selection on the changed libraries/runner: PASS.
- Bandit medium/high scan of 19 changed library modules: PASS.
- `pip check` and `git diff --check`: PASS.
- A verification pass caught an undefined logger in the rare lease-reaffirmation
  cleanup branch. It was fixed before completion, and an explicit failed-pin case
  now proves original execution uncertainty is preserved rather than masked.
- `service-identity` 24.2.0 installed; current cTrader 0.9.2 pins pyOpenSSL 24.1.0 /
  cryptography <43, whereas service-identity 26 requires cryptography >=47. The
  declared compatible range avoids silently breaking that SDK dependency stack.
  It is not a certification of that older SDK/crypto stack's overall security.

## Mandatory remaining readiness gates

1. **Pre-submit protective-order intent journal/open-algo inventory recovery is not
   complete.** Market entry/reduction intents are durable; accepted protective IDs
   are written through after receipt. Entry/reduction failure generally leaves its
   market intent unapplied, but a standalone replacement still has a POST-to-ID-write
   crash window. Keeping the old SL active is not proof that an unknown extra
   conditional order cannot remain. Implement durable clientAlgoId preclaims and
   reconcile all open/provisional protective orders before live activation.
2. Uncertain order/application recovery is intentionally operator-blocked, not fully
   automatic. No resetting/deleting journal or capital rows to force readiness.
   Complete approved client-ID/position/fill/protection reconciliation is required.
3. Real ClickHouse restart/archive/ordering behavior remains NOT TESTED. Store writes
   use lifecycle timestamps rather than consumer arrival time, but DateTime64(3)
   ties and strict monotonic revision semantics are not certified. Full account
   matching blocks mismatched recovery; it does not replace a DB fault test.
4. Nine older service-backed tests remain NOT TESTED locally. Fifteen deselections
   include six Redis tests exercised separately and those nine unavailable cases.
5. Binance/algo wire behavior is source-derived and mocked, NOT TESTED against an
   exchange. Account entitlements, server-clock handling, fills/fees, hedge behavior,
   protection race recovery and isolated approved testnet/paper soak remain gates.
6. cTrader/MT5/TradeLocker position-bound reductions/protection, account namespaces,
   API-key rotation recovery and original-venue routing are not complete. Guarded
   unavailability is not a finished integration. Default supported canonical scope
   here is one dedicated Binance account / state root, not multi-account HA.
7. OS ownership is single-host/same-directory, not fencing across machines or other
   clients. Windows lock implementation exists but Windows behavior is NOT TESTED.
8. Remote CI, supported Python matrix and independent review: NOT TESTED. No actual
   subagents/independent departments were spawned. Production-path latency,
   throughput and profitability: NOT MEASURED. Added durability/account checks are
   intentional correctness work; measure their full-path cost before optimizing.

## Operator routine and rollback

See `docs/EXECUTION_RECOVERY.md` for read-only inventory, API namespaces, capital
ownership and safe manual gates. The offline command never talks to a venue or marks
an account safe; an empty/misspelled scope is not exposure verification.

Rollback must be hunk-scoped, not `git reset` against this broad working tree. Revert
only this package's named new modules and extended execution/capital/lifecycle/live
runner hunks, preserving previous reliability/research/founder edits. Treat removal
of intent/confirmed-fill/reduce-only guards as reintroducing financial-state defects,
not as a valid mitigation. Do not migrate/delete existing runtime data as rollback.
No activation/deployment was performed, so no production rollback was attempted.


## Completion verification addendum

VERIFIED: final current-tree non-integration regression **1607 passed, 15 deselected
in 67.67s**, no warnings. Log: `tmp/backend-execution-final-verified.log`. This equals
the previous 1477-case isolated baseline plus this package's 130 new cases. The
15 exclusions are six real Redis cases separately rerun (6 passed in 0.94s) and
nine older service-backed cases NOT TESTED locally. It is not a full-suite pass.

Final targeted new-case selection: **130 passed in 2.78s**, RuntimeWarning as error.
Repository Black 611 files, changed production compileall/Flake8 F selection,
Bandit medium/high on 19 libraries, pip check and diff checks all PASS. No Redis
process remained. Production source was unchanged after this final regression;
only documentation/evidence entries were completed afterward.
