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
identity is missing/orphaned. At startup, a separate narrow recovery pass may bind
an accepted initial STOP_MARKET only when a durable owner trade ID, exact preclaim,
fresh authenticated client-algo lookup, and unique matching open trade all agree.
It writes the recovered venue ID to the durable trade snapshot before transitioning
the journal to APPLIED; a crash between those writes is idempotently recoverable.
The pass never submits, cancels or replaces an order. Replacement stops, take-profit
orders, triggered/child-fill cases, legacy preclaims without owner IDs and every
mismatch remain quarantined and block startup for manual reconciliation. The
diagnostic report always has `safe_to_resume: false`; the live guard uses explicit
reconciliation invariants, not that report field. Do not bypass quarantine based on
a report or a model recommendation.

The live runner also performs one signed Binance fee/funding history reconciliation
before allowing fresh exposure. A transport/API failure or any income row that
cannot be uniquely attributed to one internal trade keeps startup blocked; the
durable cursor remains held before unresolved rows for later operator-led
reconciliation. The background reconciler continues only after the startup gate
has been evaluated.

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
