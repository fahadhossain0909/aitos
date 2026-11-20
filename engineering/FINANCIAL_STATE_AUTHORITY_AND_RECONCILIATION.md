# AITOS Financial State Authority & Reconciliation Contract

**Status:** engineering contract and acceptance checklist; not a claim that every adapter/runtime has passed the checks below.

## 1. Authority rules

AITOS has multiple persistence and transport systems. They do not form one distributed transaction. Each store must have a narrowly defined authority boundary.

| Aggregate | Authoritative source | Local durable evidence | Non-authoritative copies |
|---|---|---|---|
| Live order acceptance, status and fills | Exchange/venue order and child-order APIs | Scoped SQLite execution intent, client IDs, result evidence | Redis events, UI cache, ClickHouse analytics |
| Live open position quantity and side | Authenticated venue account position snapshot | Lifecycle state and position/protection journals | Strategy proposals, event stream, stale portfolio cache |
| Live account equity, margin and available balance | Authenticated venue account endpoint; keep total margin equity distinct from spendable available balance | Timestamped account snapshot/telemetry | Starting-equity constants, local PnL sums |
| Live fees, funding and realized income | Venue income/commission/funding history, reconciled by stable transaction identity | Cost-adjustment ledger and immutable journal events | Estimated fee/slippage and model attribution |
| Capital reservation ownership | Scoped durable SQLite reservation ledger and its owner/scope | Reservation transitions and execution intent | Redis locks, process-local dictionaries, timer expiry |
| Paper orders, positions and realized PnL | Paper execution simulator plus its durable lifecycle/portfolio ledger | DurableTradingStateStore and journal write-through | Live venue account (must not be mixed into paper equity) |
| Market events and revisions | Versioned source event plus canonical market-data revision/provenance record | ClickHouse history and source payload identity | Redis stream delivery position alone |
| Learning/decision evidence | Decision/outcome ledger and versioned model registry | ClickHouse experience records, artifact hashes and validation reports | Latest model file without lineage/approval |

**Important distinctions**

- Redis Streams is a delivery/coordination transport, not the financial system of record.
- ClickHouse is an analytical/history store. A successful insert does not make a cross-store lifecycle transaction atomic.
- SQLite execution intents/reservations provide local durable ownership and uncertainty tracking. They are not proof that the venue account is flat.
- The operator UI is a read-only projection and must clear stale/unavailable financial state rather than silently retain or synthesize it.
- Paper and live equity, fills, reservations, and ledgers must remain separate.

## 2. Live order lifecycle and recovery

The canonical ordering is:

1. Validate market/strategy proposal and fresh portfolio/account evidence.
2. Calculate positive finite stop-derived risk and obtain capital authorization.
3. Acquire/pin the durable capital reservation.
4. Commit a scoped SQLite execution intent and stable client order ID before the venue side effect.
5. Submit once. An ACK/NEW response is not a fill.
6. Resolve terminal order status, actual child fills, commissions and fees using venue evidence.
7. Apply the actual filled quantity/price to lifecycle state and install/verify protective orders.
8. Verify authenticated account positions, open orders and conditional/algo orders against local state.
9. Durably write the lifecycle/journal/analytics records and only then mark the intent APPLIED or release owned capital.

Any timeout, cancellation, ambiguous response, incomplete pagination, missing protective child evidence, mismatch or cross-store failure remains quarantined. Never retry an ambiguous entry POST, assume an ACK is a fill, release a reservation because a timer elapsed, or infer flatness from a local journal alone.

The current execution recovery tooling is deliberately diagnostic/read-only. Its `safe_to_resume: false` output is not a deficiency to work around; it is the correct boundary until an operator has reconciled the venue and local evidence.

## 3. Reconciliation matrix

| Check | Evidence required | Pass condition | Failure action |
|---|---|---|---|
| Orders | Complete venue open/terminal order inventory, scoped local intents and stable client IDs | Every local active intent has one matching venue lifecycle or documented terminal outcome; no unknown venue order | Quarantine affected symbol/account; block fresh risk |
| Fills | Parent/child order IDs, cumulative executed quantity, average fill price, trade IDs | Local fills equal venue fills within venue precision; partial fills are applied exactly once | Keep intent unresolved; no synthetic fill |
| Positions | Complete account position snapshot, instrument, side, quantity, entry/mark, margin mode | Venue quantity/side reconciles to local lifecycle within contract precision | Block new entries; preserve reservations; operator review |
| Protective orders | Standard open orders plus conditional/algo inventory, child-order status/fills | Every open position has correctly bound live protection, or explicitly approved documented exception | Do not resume new exposure; reconcile before release |
| Cash/equity | Total margin balance, available balance, unrealized PnL and timestamp | Correct field used for equity vs spendable margin; no missing/non-finite values | Fail closed; do not substitute starting balance |
| Fees/funding | Venue income history, commission assets, funding transactions and conversion price | Unique transaction IDs applied idempotently; costs reconcile to ledger | Keep settlement pending; revise attribution only once |
| Reservations | Owner/scope, reservation amount, risk budget, associated active intent/position | Each confirmed exposure has an owned reservation that fully covers required capital/risk | Retain larger reservation; missing/under-sized reservation blocks readiness |
| Market history | Exchange/source identity, event ID, source timestamp, revision, ingest timestamp | Duplicate/revised events are idempotent and point-in-time ordering is preserved | Mark data gap; do not silently backfill with future data |
| Model/decision evidence | Decision ID, model version/hash, feature snapshot, training cutoff, OOS report, approval record | Decision can be reproduced and candidate promotion is governed | Keep candidate shadow-only; rollback to approved champion |

## 4. Restart acceptance scenarios

These must be tested against an isolated paper/testnet environment with fault injection. They are not satisfied by unit tests alone.

- Crash before intent commit: no venue request was sent; safe to start after confirming no external side effect.
- Crash after intent commit but before request: startup must discover unresolved SUBMITTING and require reconciliation; no blind retry.
- Crash after venue accepted but before response: client-ID lookup and full account/order reconciliation required.
- Crash after partial fill: local state must reflect actual partial exposure and keep protective/capital ownership.
- Crash after fill but before stop placement: startup must quarantine; no new entry until protection is restored and verified by an authorized recovery procedure.
- Crash after protective POST but before local acknowledgement: preclaim and exchange algo/child evidence must be reconciled; no assumption of success.
- Crash after venue close but before local close commit: venue-flat evidence and terminal fills must be applied idempotently before reservation release.
- Redis restart/consumer replay: duplicate events must not duplicate financial effects; Redis delivery state cannot override SQLite/venue authority.
- ClickHouse outage: local durable intent and lifecycle ownership remain intact; analytics write retry/reconciliation must not falsely mark the whole operation applied.
- Restore from backup: restore all scoped SQLite journals and ClickHouse history from a consistent backup set, then reconcile live account state before allowing new entries.

## 5. Paper runtime rules

Paper runtime must restore:
- open and closed lifecycle records;
- realized PnL, fees/funding adjustments and equity ledger;
- peak equity/drawdown and daily risk counters;
- pending order/partial-fill state if simulated execution supports it;
- market subscription readiness for every restored open position.

A paper process restart with missing ledger or stale/missing mark data is not allowed to initialize a fresh starting-equity balance and continue as if the account were new. The runtime must expose a degraded/unavailable state and block new entries until restore/reconciliation succeeds.

## 6. Strategy and capital boundary

`StrategyEngine` is a proposal/allocation layer. It is not the production order-dispatch path. Strategy results must be converted to the canonical opportunity contract and submitted through the existing `TradeLifecycle.submit_opportunity` boundary, where hard limits, CapitalGateway, reservations, execution intent and venue adapters remain authoritative. No strategy module may call a venue executor directly or create a parallel risk authority.

Until a typed, tested bridge exists, StrategyEngine outputs are research/shadow evidence only and must not be described as active live execution.

## 7. Release evidence

Every acceptance record must identify:
- exact source commit SHA and PR;
- workflow run ID and individual job conclusion;
- platform/runtime versions and dependency lock state;
- database schema/migration version and restore snapshot ID;
- testnet/paper account mode (never real credentials in logs);
- scenario, expected result, actual result and retained artifact/log digest;
- unresolved exceptions and explicit owner.

A green CI run is necessary but not sufficient. A paper soak, testnet lifecycle, clean-machine desktop acceptance, restore drill and independent security review are separate release gates. No section of this document authorizes live trading or production deployment.
