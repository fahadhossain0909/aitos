# Execution integrity and recovery operations

## Safety status

The canonical live runner now uses durable market-order intent claims, confirmed
terminal execution, write-through lifecycle state, full-account quantity matching
and pinned live capital ownership. This is **not permission to enable live trading**.
Exchange integration, real ClickHouse recovery, protective-intent crash recovery,
paper soak and production-path latency remain separate readiness gates.

Use one dedicated trading account and one `MODEL_DATA_DIR` for a runtime. The local
OS lock prevents a second canonical live runner sharing that state root. It is not
an exchange-wide/distributed lock and cannot prevent another application/manual
trader, another machine or another model directory from changing the account.
Account namespace support across all historical/runtime tables is not certified.

## What submission means

1. A logical request is bound to a stable client order ID.
2. The scoped SQLite `SUBMITTING` intent is committed before a market POST.
3. `newOrderRespType=RESULT` requests actual execution evidence. ACK/NEW is not fill.
4. One client-ID lookup, and when necessary cancel plus final lookup, bounds normal
   NEW/partial settlement. A network timeout/absent lookup is not a rejection and
   is never followed by an automatic second POST or cross-venue failover.
5. Only valid terminal quantities/prices become a local position/reduction. A
   cancelled terminal order with an actual partial fill still creates exposure.
6. Confirmed execution stays `CONFIRMED` until the lifecycle has installed the
   position, recorded/protected it, checked the account snapshot and durably written
   the resulting state. Only then is its intent `APPLIED`.

Known explicit no-acceptance errors or terminal zero-fill evidence produce
`REJECTED`. Anything uncertain remains unresolved, retains capital ownership and
blocks new exposure. Reduce-only de-risking is not globally blocked just because
another intent is unresolved; individual uncertain lifecycle transitions are latched
against duplicate execution and require reconciliation.

Caller cancellation does not cancel the entire live capital-to-protection transition.
Shutdown stops new live submissions and drains owned submissions before removing
critical persistence callbacks. Hard process death instead relies on durable evidence
and a fail-closed next startup; it is not transparent automatic recovery.

## Current Binance conditional protection API

New Binance TP/SL placements use `/fapi/v1/algoOrder`, `algoType=CONDITIONAL`,
`triggerPrice` and `clientAlgoId`. IDs are persisted as `algo:<algoId>` so they cannot
be confused with ordinary market-order IDs. `TRIGGERED` or `FINISHED` is not proof of
a trade fill: the linked `actualOrderId` must supply authoritative quantity and price.
A child still NEW remains pending; terminal partial child execution is reconciled
at its actual quantity, without a second market reduction.

Old numeric persisted IDs still query/cancel `/fapi/v1/order`. An explicit
`conditional_orders_api="legacy"` is retained for providers implementing the old
Binance-compatible contract; Alfax preserves that compatibility setting. This is not
certification that a third-party venue supports either API. No deprecated connector
was added as a runtime dependency.

Protective replacement keeps the existing SL until its replacement is accepted and
durably recorded. Old SL IDs awaiting verified cancellation are retained in
`agent_consensus._pending_stop_cancellations`; such unresolved state blocks fresh
entries. Confirmed terminal venue evidence now transitions the matching protective
journal row to `TERMINAL`, retaining terminal status and executed quantity. This
prevents a successfully replaced/canceled old stop from being mistaken for a
currently resting order. Terminal state is recorded only after a signed venue status
response; an ambiguous cancellation remains unresolved.

## Protective acceptance identity binding

The local protective-intent journal validates an accepted resting-order result against
its exact pre-submit claim before changing `SUBMITTING` to `PROTECTED`. The binding
checks the claimed symbol and position side, exact returned client ID, a strictly
numeric exchange order identity (including the `algo:` namespace), finite positive
trigger price equal to the claimed trigger, and zero filled quantity. The update is
conditional on the row still being `SUBMITTING` and verifies exactly one row changed.
A mismatched or repeated acknowledgement leaves the original intent unresolved.

This closes a local evidence-integrity gap; it does **not** automatically resume a
process that died between the exchange POST and the local acknowledgement write.
A signed `clientAlgoId` lookup is now available to the read-only diagnostic path,
including child-order status/fill lookup when Binance returns an actual child ID.
The next startup still fails closed at the durable `assert_ready()` boundary until
an operator has reconciled the preclaim; the diagnostic never changes journal state
or authorizes resume. Terminal/child-order and account-position reconciliation
remain independent requirements.

## Inspect quarantine without exchange access

The SQLite file is under `MODEL_DATA_DIR/execution_intents.sqlite3`. Its scope is the
SHA-256 fingerprint used by `prepare_live_executor` for environment + API-key identity;
no API key/secret is stored in the journal. Fingerprints do not provide complete
account identity or automatic API-key-rotation reconciliation.

```sh
python -m aitos.execution.recovery \
  --database /absolute/path/to/execution_intents.sqlite3 \
  --scope THE_EXISTING_SCOPE_HASH
```

This command opens an existing SQLite file read-only. It never connects to a venue,
changes state, clears quarantine or releases capital. Exit 1 means unresolved local
records exist; exit 2 means the journal could not be read. Exit 0 means only that no
unresolved record was found for the supplied scope—not that the account is safe.
`venue_exposure_verified` remains false, including when a scope is misspelled/empty.

Never delete an intent, change a client ID, remove a pinned reservation or clear a
model directory to get past the startup gate. An operator must reconcile client IDs,
actual positions, open conditional orders, fill/cost history and durable lifecycle
state first. There is deliberately no automatic reset/"mark safe" CLI.

## Capital and persistence

Live portfolio equity now comes from authenticated Binance account information
/fapi/v3/account 's `totalMarginBalance` field, which includes unrealized
PnL. The separate `get_account_balance()` contract continues to expose
`availableBalance` for spendable-margin callers; it is not used as portfolio
equity or as the drawdown denominator. Missing, non-finite or negative total
margin equity fails closed instead of silently substituting available margin.

Live capital has a separate `capital_live_<scope>.sqlite3` ledger, stable owner
identity and pinned submitted/open exposure. Paper authorization lease behavior is
preserved. A timer or process crash does not make uncertain live capital spendable.
The canonical runner holds its OS ownership lock while using the stable owner.

After known protective fills are reconciled and the entire account/local quantity
snapshot matches, the runner restores conservative reservations for real open
exposure and releases owned flat leases left after a durable close. It does not steal
another owner's reservation. Live close/partial-close requires actual reduce-only
execution before local accounting; a partial exit is not reported as a completed close.

The ClickHouse write-through callback archives a terminal trade before deleting its
open row. SQLite application acknowledgement follows successful critical write and
account verification. These are ordered operations across two stores, not an atomic
cross-database transaction. Failure is quarantined, not treated as success.

## Unsupported/uncertified adapters

cTrader/MT5/TradeLocker do not currently implement position-bound reduce-only close
and generic protective-order mapping. Their lifecycle capability now requires real
exchange execution; unsupported reductions fail before a broker request, rather than
using local PnL or reversing/opening a position. TradeLocker ACK no longer fabricates
a fill. Live capital routing cannot fail over after an uncertain submission and
cannot choose an arbitrary venue for a reduction whose original venue is unbound.

These guards are not completed broker integrations. cTrader SDK/Twisted dependencies
also require a coordinated upgrade/security review; `service-identity` restores its
hostname-verification dependency without violating the current SDK's cryptography pin.

## Verification gates before live readiness

- Test real Binance-compatible request/response behavior without placing real orders
  in this environment; then approved isolated testnet/paper validation separately.
- Implemented narrow restart recovery for accepted initial STOP_MARKET orders: durable preclaims carry `owner_trade_id`; the runner performs an authenticated client-algo lookup, checks exact account scope, trade identity, symbol/side/quantity/trigger/reduce-only semantics, persists the recovered stop ID to the trade snapshot, then marks the intent APPLIED. Fault-injection and idempotent restart tests cover the trade-write/journal-ack boundary. Replacement stops, take-profit orders, triggered/child-fill cases, legacy preclaims without owner IDs and mismatches remain quarantined; they are not blindly retried or automatically mutated.
- Exercise real ClickHouse failure/restart/archive ordering with isolated data.
- Perform complete account/position/protection recovery fault matrix and paper soak.
- Run remote CI, supported Python/OS matrix and independent review.
- Measure full-path latency/throughput with all calculations and safety checks enabled.

Detailed evidence: `engineering/EXECUTION_RECOVERY_IMPLEMENTATION_2026-11-07.md`.

## Offline protective inventory comparison (2026-11-07)

The existing read-only inventory CLI optionally accepts `--protection-evidence`
pointing to an exported JSON file:

```sh
.venv/bin/python -m aitos.execution.recovery \
  --database /path/to/existing/intents.sqlite3 \
  --scope YOUR_EXISTING_ACCOUNT_SCOPE \
  --protection-evidence /path/to/exported-open-algos.json
```

The file must declare `scope` matching the existing journal scope,
`captured_at` as a timezone-aware ISO timestamp, `complete: true`, a nonempty
`symbols` array covering journal protection symbols, and an `orders` array.
Orders use current algo fields `clientAlgoId`, `algoId`, `symbol`, `side`,
`positionSide`, `reduceOnly`, `orderType`, `algoStatus`, `quantity`, `triggerPrice`.
For one-way positions `positionSide` must be BOTH and reduceOnly true; hedge
orders need the matching LONG/SHORT position side. Quantities/triggers compare
as finite positive decimal values, not float tolerances.

Freshness is limited to 120 seconds by default. Cross-account, incomplete,
duplicate-identity, invalid-symbol-coverage, future or stale envelopes fail closed.
The CLI opens the existing SQLite database read-only, bounds file/row counts and
never creates a missing journal. Reports contain whitelisted findings, not raw
order exports or credentials.

Statuses distinguish `unapplied_match`, `applied_order_present`, `mismatch`,
`unowned_order` and `not_in_open_inventory`. Missing does not mean no exposure:
it may require querying terminal algo state and the actual child fill. Historical
APPLIED orders can legitimately be absent after fills/cancellation; the report
does not infer that lifecycle context from an open-order snapshot.

The offline analyzer targets modern algo exports, not a certified legacy/broker
inventory adapter. The authenticated runtime diagnostic additionally reads the full position
snapshot and ordinary open-order inventory, checks every active local trade has
its matching algo stop in the venue inventory, and blocks live scans when positions
differ, an entry order lacks a validated stop-risk projection, or a protective
identity is missing/orphaned. At startup, a narrow recovery pass may bind an accepted initial STOP_MARKET or
TAKE_PROFIT_MARKET only when a durable owner trade ID, exact preclaim, fresh
authenticated client-algo lookup, and unique matching open trade all agree. Stop
identity is written to `sl_order_id`; TP identity is appended idempotently to
`tp_order_ids`. The durable trade snapshot is written before transitioning the
journal to APPLIED, so a crash between those writes can be completed on the next
restart. This pass never submits, cancels or replaces a venue order.

Replacement-stop recovery is still deliberately quarantined: the current intent
journal does not yet encode a durable old-stop/new-stop handover state and
cancellation acknowledgement, so startup must not guess whether the old stop is
still protective or whether a replacement is safe to activate. Triggered algo
orders are not fills; the authenticated diagnostic can look up the linked child
order and report its status/executed quantity, but it does not yet apply cumulative
or partial child fills to the lifecycle ledger. Those cases, legacy preclaims
without owner IDs and every mismatch remain blocking/manual-reconciliation cases.
The diagnostic report always has `safe_to_resume: false`; the live guard uses
explicit reconciliation invariants, not that report field. Do not bypass quarantine
based on a report or a model recommendation.

## Remaining restart and acceptance gates (2026-11-20)

| Gate | Current evidence | Status |
|---|---|---|
| Initial STOP recovery | Owner-bound authenticated identity, durable snapshot-before-journal ordering, restart idempotency tests | Implemented; CI validation pending for current PR head |
| Initial TP recovery | Owner-bound authenticated identity, idempotent `tp_order_ids` persistence, focused restart test | Implemented; CI validation pending for current PR head |
| Replacement protective-order handover | Durable STOP and TAKE_PROFIT make-before-break journal/recovery, exact owner and venue identity, old-order status checks around cancel, snapshot-before-ack ordering | Code and fault-injection tests added; current-head CI and authenticated testnet lifecycle remain required |
| Triggered algo / child partial fills | Decimal cumulative quantity/quote delta validator with strict algo-child linkage and monotonic watermark inputs | Validator and regression tests added; TradeLifecycle application, durable snapshot watermark and restart integration remain open; fail-closed quarantine |
| Venue + SQLite + ClickHouse crash drill | Isolated fault injection now covers replacement handover persistence failure before cancellation and after cancellation acknowledgement | Partial boundary coverage only; full coordinated three-store crash/restart drill not yet accepted |
| Authenticated testnet lifecycle | Hosted paper canary previously returned HTTP 451; no verified authenticated end-to-end testnet lifecycle receipt | Blocked by runner/network access; no mainnet fallback |
| StrategyEngine typed bridge | StrategyEngine remains proposal/allocation/shadow-only pending typed Opportunity and validated risk/stop/account contracts | Intentionally not live-wired |

A complete multi-store drill must inject termination after each durable boundary
(venue acknowledgement, SQLite intent/result transition, ClickHouse trade snapshot,
income cursor, and ClickHouse analytics/event append), restart with the same account
scope, and prove no duplicate venue side effect, no lost/duplicated cumulative fill,
no cursor advancement ahead of durable financial application, and no fresh exposure
while any store is unavailable or contradictory. It must compare venue positions,
open ordinary orders, open algo orders and terminal child-order fills against the
restored SQLite journal and ClickHouse trade/closed-ledger state. Until that drill
and authenticated testnet lifecycle acceptance pass, the feature remains gated.

The live runner also performs one signed Binance fee/funding history reconciliation
before allowing fresh exposure. Each income row is applied idempotently through
the lifecycle and written through to the durable open/closed trade snapshot before
the exchange-income cursor can advance; the subsequent event is a replayable
notification, not the only persistence path. If the process fails after the trade
write but before cursor advancement, the stable Binance transaction ID makes the
retry idempotent. A trade-write, event-publication or transport/API failure, or any
income row that cannot be uniquely attributed to one internal trade, keeps startup
blocked; the durable cursor remains held before unresolved rows for later
operator-led reconciliation. The background reconciler continues only after the startup gate has been evaluated.
If either financial or restored-position readiness is false, the runtime stays in
manage-only mode: market ingestion, lifecycle price/EIE management, exchange
protection reconciliation and existing-position supervision remain active, while
the fresh-exposure scan is disabled until an operator restarts after remediation.

## Closed-ledger integrity and SQL verification (2026-11-07)

Closed-history and realized-PnL restore now require a unique latest retained
payload, matching trade identity, terminal state and finite PnL/cost evidence.
Corrupt or ambiguous closed records block recovery instead of silently shrinking
realized PnL. Nonfinite JSON writes are rejected before insert.

An actual embedded ClickHouse test exposed a shadowed aggregate alias: naming
`any(payload_json)` as `payload_json` caused the neighboring `uniqExact` to expand
the alias as a nested aggregate (`ILLEGAL_AGGREGATION`). Runtime and closed readers
now use the distinct `latest_payload` alias. Unit mocks alone had not proved SQL
validity; optional embedded engine checks now cover the actual query.

```sh
.venv/bin/python -m pip install -r tests/requirements-clickhouse-embedded.txt
.venv/bin/python -m pytest tests/test_clickhouse_embedded_ledger.py \
  -m integration --timeout=45 -q
```

This runs isolated temporary fixture databases only. The package reports embedded
ClickHouse 26.9.2.1; it is not certification of the deployment server version,
ClickHouse Connect transport, durability/restart, multi-writer ownership or
strict revisions. Previously merged/discarded timestamp conflicts cannot be
reconstructed by these readers. Legacy malformed financial data requires review,
not a catch-and-ignore or ledger deletion workaround.

Console history/performance queries have query-local time/read-byte/memory limits
and throw on read overflow instead of returning partial financial reports.
Startup/full recovery is separate and does not use the UI output bounds. No
production settings, migrations or data cleanup were applied during verification.


## Authenticated read-only algo inventory reconciliation

`BinanceFuturesOrderExecutor.get_open_algo_orders()` now fetches the complete USD-M conditional inventory through the signed `GET /fapi/v1/openAlgoOrders` endpoint. `get_open_orders()` separately reads ordinary orders through `GET /fapi/v1/openOrders`; conditional algo orders must not be assumed to appear in that response. Both readers validate the entire returned list and reject malformed, duplicate, non-open or cross-symbol rows rather than silently dropping them.

`aitos.execution.protection_reconciliation.audit_exchange_protection_inventory()` combines the executor's authenticated open-algo response with the matching scoped `ExecutionIntentStore` reader. It is read-only and reports `AUTHENTICATED_READ_ONLY_EXCHANGE_API` provenance. A matching `clientAlgoId`/`algoId`/symbol/side/type/quantity/trigger remains an `unapplied_match`, not a recovery approval. Missing open inventory remains ambiguous and requires terminal algo status, child-order fill evidence and account-position reconciliation. The report always has `safe_to_resume: false`, `account_exposure_verified: false`, and no automatic actions. It never writes to the journal or touches venue orders.

## Strict pending-entry exposure projection

`aitos.exchange.binance_pending_exposure.pending_exposures_from_open_orders()` converts a complete standard USD-M `openOrders` response into `PendingOrderExposure` rows. It accounts only for unfilled remainder after partial fills; excludes reduce-only, close-position and hedge-mode closing legs; and requires fresh mark, side-correct protective stop and leverage inputs. The open-order and risk-input timestamps must both be timezone-aware, within five seconds of the supplied current time, and within two seconds of each other. Risk is linear-contract remaining quantity multiplied by mark-to-stop distance. This adapter is not for inverse/coin-margined contracts and is not yet connected to the live account tracker: that provider's authoritative snapshot owner and lifecycle remain an explicit acceptance gate.


## Replacement TP handover and child-fill validation follow-up (2026-11-20)

The protective handover journal now stores an explicit `order_type` with an
additive SQLite migration. Existing rows migrate as `STOP_MARKET`; the same
guarded PREPARED → NEW_ACCEPTED → OLD_CANCEL_REQUESTED → OLD_CANCELLED → APPLIED
state machine can now recover `TAKE_PROFIT_MARKET` replacements. TP recovery
requires the exact prior TP journal owner and quantity, validates the new
venue algo identity and trigger, replaces the exact TP ID in the restored trade
snapshot, and only then confirms cancellation of the old TP. It does not
replace a TP that has already partially filled or whose ownership is ambiguous.

`TradeLifecycle._replace_exchange_side_take_profit()` provides the matching
make-before-break live operation for legacy static-TP positions. It requires an
untouched NEW old TP, retrieves its exact original quantity from the scoped
journal, submits a deterministic owner-bound replacement, persists the new ID
and level before cancellation, verifies the old order is terminal, persists
the cleared pending-cancellation marker, and only then acknowledges the new
intent. EIE-managed TP checkpoints are deliberately excluded because an
exchange-side TP would bypass HOLD/MANAGE/EXIT arbitration.

The triggered child validator now requires a confirmed TRIGGERED/FINISHED
conditional algo, exact algo-to-child linkage, symbol/side/type/position-side
identity, bounded executed quantity, monotonic cumulative executed quantity
and quote, and positive incremental quote for every new fill. The incremental
fill price is `delta cumQuote / delta executedQty`; the venue's lifetime
average price is not incorrectly applied to a partial delta.

**Important remaining boundary:** the child validator is a pure evidence
contract. It is not yet wired to mutate `TradeLifecycle` or to persist a
cumulative watermark in the same durable trade snapshot. Therefore triggered
child fills remain quarantined and must not be treated as applied. The
replacement handover fault-injection tests exercise crash/restart at the
ClickHouse snapshot boundary before cancellation and after cancellation ACK,
but they do not substitute for the full Venue + SQLite + ClickHouse coordinated
fault-injection drill. Authenticated Binance demo testnet acceptance remains
blocked when the hosted runner receives HTTP 451; no mainnet fallback is
permitted.


## Triggered protective child fill application (2026-11-20)

Startup now inspects only protective algo IDs already owned by restored open
trades. A triggered/finished algo is fetched again through the authenticated
client-algo lookup, then its actual child order is read from the signed order
endpoint. The child is accepted only when algo/child ID, symbol, side, order
type, position side, reduce-only semantics, original quantity and cumulative
quantity/quote are consistent.

The lifecycle computes the unapplied quantity and quote deltas using Decimal.
It applies partial reductions through the existing PnL/quantity/risk ledger, or
routes a terminal full-size fill through the confirmed-close path. The
`_applied_protection_child_fills` marker is written in the same durable trade
snapshot as the updated position. Only after that write does SQLite advance its
per-`(scope, algo_id, child_order_id)` mirror watermark. If a crash occurs
between those writes, the restored trade marker prevents a duplicate reduction
and the SQLite mirror can be replayed forward. If SQLite is ahead of the
restored trade marker, recovery fails closed.

For a terminal partial stop fill, a fresh stop is installed against the
remaining quantity using the existing make-before-break stop handover before
the old triggered algo is retired. For a still-active partial child, the
position is updated but remains quarantined with fresh entries disabled until
the child reaches a terminal state. Terminal TP children are removed from the
trade's TP ID/level pair after the fill has been durably applied.

The startup account-reconciliation guard now degrades to manage-only instead
of terminating the process when venue exposure cannot be proven. In that
state, existing-position price/EIE management and reconciliation remain
running; reservation release/promotion is skipped and new entries remain
disabled. This is a safety fallback, not a substitute for an operator resolving
the venue/local discrepancy.


## Three-store crash/restart matrix and strategy shadow governance (2026-11-20)

### Coordinated integration harness

The opt-in integration test
`tests/integration/test_execution_recovery_three_store.py` uses:

- A real, isolated ClickHouse database created and dropped for the test.
- A real SQLite `ExecutionIntentStore` on a temporary filesystem.
- The production `BinanceFuturesOrderExecutor` request/signature/response path,
  pointed only at a deterministic local Binance-compatible HTTP fixture. The
  fixture verifies the API-key header and HMAC signature and returns controlled
  algo/child snapshots. It never forwards a request to Binance and rejects any
  non-GET route.
- Full lifecycle application and process-object replacement, restoring only
  from ClickHouse trade snapshots and the durable SQLite journal.

The current fault matrix injects failure before the ClickHouse trade snapshot
write and after that snapshot but before the SQLite child-fill watermark ACK.
Both paths discard in-memory lifecycle state, reconstruct a new lifecycle from
the durable snapshot, replay authenticated-style venue evidence, and assert
that cumulative quantity/PnL are applied exactly once. A separate opt-in
read-only smoke test can query authenticated Binance Futures demo-testnet
positions and open algo inventory; it never submits, cancels, or replaces an
order.

Run the ClickHouse-backed matrix against an isolated ClickHouse server:

```sh
AITOS_ENABLE_THREE_STORE_FAULT_TESTS=1 \
AITOS_FAULT_CLICKHOUSE_HOST=127.0.0.1 \
AITOS_FAULT_CLICKHOUSE_PORT=8123 \
pytest -q tests/integration/test_execution_recovery_three_store.py
```

The dedicated GitHub Actions workflow provisions ClickHouse and runs this
matrix plus the repeated-restart/ambiguous-child and typed Opportunity tests.
The external demo-testnet read-only preflight is separately opt-in through
`AITOS_ENABLE_BINANCE_TESTNET_READONLY=1` and dedicated
`BINANCE_FUTURES_TESTNET_API_KEY/SECRET` credentials.

**Evidence boundary:** the local HTTP fixture validates the actual Binance
executor's signed HTTP contract, not Binance's remote matching engine or
exchange-side persistence semantics. The testnet smoke test verifies read-only
connectivity only. An approved isolated testnet order/protection lifecycle
and real venue-side crash reconciliation remain separate acceptance gates.

### Repeated restart and ambiguous child handling

Child-fill application now resolves exactly one durable protective intent by
venue algo ID and owner trade ID before applying a fill. The actual child
`origQty` must exactly equal the quantity recorded in that intent; matching
symbol/side and `executedQty <= origQty` alone are insufficient. Client algo ID,
owner, symbol, order type, reduce-only semantics, position side, and algo-to-child
linkage are all validated. Missing, duplicate, cross-owner, or quantity-mismatched
evidence remains quarantined.

Regression tests replay a still-triggered partial child across four fresh
lifecycle instances and exercise mismatched client ID, algo-child link, child
order ID, symbol, side, original quantity, position side, and duplicate durable
algo identity. No ambiguous case is allowed to change the trade snapshot.

### Typed StrategyEngine to canonical Opportunity bridge

`aitos/strategy/opportunity_bridge.py` converts a StrategyCycle shadow proposal
to the canonical `aitos.models.trade.Opportunity` only when a typed explicit
protection plan provides directionally valid stop and take-profit levels. It
runs the candidate through an isolated `CapitalGateway` supplied by a factory,
then attaches a typed `OpportunityRiskReceipt` containing the approved shadow
capital and risk budget. The shadow gateway must not share the live reservation
ledger. Duplicate canonical symbols, missing capital requests/allocations,
missing protection, invalid confidence, and CapitalGateway vetoes are rejected.

Even an accepted canonical Opportunity is always `is_production=False`,
`approved_by=None`, `execution_authorized=False`, and
`execution_eligible=False`. This bridge does not call TradeLifecycle, create
orders, reserve live capital, or change StrategyEngine's existing run-cycle
behavior. A later live integration requires a separately reviewed execution
authorization and account/venue-specific contract; this bridge is not that
authorization.

The typed protection plan also requires an explicit loss-probability estimate;
strategy confidence is not silently reinterpreted as a loss probability. The
bridge limits accepted capital to the smaller of the strategy request,
StrategyEngine allocation and CapitalGateway allocation, then caps projected
risk by the upstream allocation's own projected-loss amount. A strategy with
multiple simultaneous instrument intents is rejected until it supplies
per-instrument allocations; one strategy-level budget is never reused for each
intent. Shadow and live CapitalGateway reservation stores are checked by object, connection
and SQLite database path identity before review.

The bridge additionally requires a typed `StrategyPortfolioRiskContext` with a
non-empty snapshot identity, current/peak equity, open-position risk, pending
order risk, side/instrument identity, correlation, daily PnL and loss-streak
evidence. Its observation timestamp must be within five seconds of the
StrategyContext cycle timestamp. Missing, stale or malformed account-risk
evidence fails closed before a canonical Opportunity is emitted.


## Coordinated three-store fault-injection acceptance (2026-11-21)

The opt-in integration workflow is
`.github/workflows/three-store-recovery-integration.yml`. Its first job
starts a disposable real ClickHouse server, creates a unique database per test,
uses the real SQLite `ExecutionIntentStore`, and exercises the production
`BinanceFuturesOrderExecutor` signed-request/HMAC path against a deterministic
local Binance-compatible HTTP protocol fixture. The fixture is intentionally
not represented as the Binance matching engine: it allows repeatable crash
injection without placing orders on any exchange.

The integration matrix injects failure at both durable ordering boundaries:

1. ClickHouse trade snapshot write fails after the venue reports a triggered
   protective child fill.
2. The ClickHouse snapshot succeeds, but the SQLite child-fill watermark write
   fails.

Each case discards in-memory lifecycle objects, restores exclusively from the
ClickHouse snapshot and SQLite journal, then performs four cold-start recovery
passes. Quantity, realized PnL and cumulative quantity/quote watermark must
remain stable after the first accepted reduction. The ambiguous-evidence matrix
also exercises foreign client algo identity, algo/child linkage mismatch,
wrong child ID, symbol, side, original quantity, position side, overfilled
quantity, unknown status and negative cumulative quote. Each must remain
quarantined without writing a trade snapshot or advancing the SQLite watermark.

The workflow's manual `workflow_dispatch` job performs authenticated,
read-only Binance Futures demo-testnet inventory checks. It requires the
dedicated `BINANCE_FUTURES_TESTNET_API_KEY` and
`BINANCE_FUTURES_TESTNET_API_SECRET` GitHub secrets; it fails rather than
silently passing when they are absent. It reads positions and open algo orders
only. It does not submit, cancel, amend or trigger any exchange order.

Run locally with a disposable ClickHouse instance:

```bash
AITOS_ENABLE_THREE_STORE_FAULT_TESTS=1 \
AITOS_FAULT_CLICKHOUSE_HOST=127.0.0.1 \
AITOS_FAULT_CLICKHOUSE_PORT=8123 \
AITOS_FAULT_CLICKHOUSE_USER=default \
AITOS_FAULT_CLICKHOUSE_PASSWORD= \
PYTHONPATH=. pytest -q --timeout=120 \
  tests/integration/test_execution_recovery_three_store.py
```

This is coordinated ClickHouse/SQLite/venue-adapter fault evidence, not yet
authenticated venue write-side acceptance. The local venue fixture validates
the real executor's signed HTTP protocol but does not prove Binance's external
matching-engine behavior. A separate dedicated demo-testnet account, approved
test-order budget, isolated runner and explicit operational authorization are
still required before an entry/protection/partial-fill/exit/restart write-side
acceptance run. Mainnet fallback is prohibited.


### Background scheduler ordering

The periodic `ReconciliationScheduler` now invokes the same authenticated,
durable protective-child recovery before ordinary `TradeLifecycle.reconcile_trade`
status handling. This is required because the older generic path could observe
an already-triggered child and issue another market reduction even though the
venue had already executed part or all of the protective quantity.

A child touched by the recovery pass is excluded from ordinary order-status
exit handling for that pass. An active partial child, ambiguous child, missing
durable intent journal or recovery exception sets the account reconciliation
gate and prevents new entries; the affected trade is not sent through a second
close path. Other unaffected trades continue to receive ordinary supervision.
Startup/manual and periodic passes share an async lock, preventing overlapping
reconciliation passes from racing the same durable child watermark. Authenticated
child inventory polling is throttled to a bounded 5–30 second interval even
when the main live scan calls `run_once` more frequently. Algo-protected trades
never fall through to generic `reconcile_trade` status handling between those
checks; non-algo legacy orders retain the existing reconciliation path. Health
and reconciliation events expose applied/blocked child counts and affected
trade IDs. The account gate is intentionally not auto-cleared by this scheduler.


The SQLite durability gate also starts a separate Python process, commits a
child-fill cumulative watermark, terminates that process with `os._exit`, and
opens a new journal instance to verify the committed identity, quantity and
quote survived an ungraceful process exit. The ClickHouse recovery matrix now
closes and recreates both the ClickHouse client and `DurableTradingStateStore`
for every cold-start pass, and reconstructs `ExecutionIntentStore` from the
same SQLite file rather than reusing the prior journal object.

Startup recovery additionally quarantines malformed JSON rows and duplicate
durable algo-order identities. In the duplicate case, neither claimant is
selected as the owner; the matching order ID is removed from the recovery map.
A future-dated portfolio risk snapshot is also rejected, even if it falls
within the nominal clock-skew tolerance.
