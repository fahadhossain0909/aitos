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
entries. Market-order journal coverage is not a complete durable protective-order
intent ledger: a crash between a protective POST and its ID write still requires
an open-algo-order inventory/reconciliation extension before live readiness.

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
- Implement and fault-test pre-submit protective intents/open-algo inventory recovery.
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

This analyzer currently targets modern algo exports, not a certified legacy/broker
inventory adapter. It does not fetch an export, independently authenticate its
completeness/account, verify positions, reconcile child fills, acknowledge IDs,
clear intents, release capital or submit/cancel replacements. Its result always
has `safe_to_resume: false`. When a protection audit is requested the CLI exits 1
rather than signaling a successful readiness certificate, even for matching rows.
Use the existing approved recovery process; do not bypass quarantine based on a
report or a model recommendation.

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
