# AEP-1 Phase 1 — Contract, Risk and Financial Authority Audit

Date: 2026-11-19
Status: source-level audit; no runtime or production certification.
Baseline inspected: current `main` source as of this audit; PR #260 carries this audit forward on its feature branch.

## Executive decision

**Do not collapse the similarly named contracts into one Python class.** Source inspection shows that the names overlap, but the objects belong to different bounded contexts and carry different semantics:

- `decision.MarketSnapshot` is a timestamped, versioned ADL evidence snapshot.
- `strategy.MarketSnapshot` is a compact strategy feature/quote view.
- `strategy.ExecutionIntent` is a strategy proposal.
- `markets.adapters.ExecutionIntent` is an adapter-facing venue-neutral execution request.
- `intelligence.autonomy_pipeline.ExecutionIntent` is an advisory/audit record produced by a separate autonomy pipeline; it is not shown to be the order-dispatch contract.

Bulk aliasing, renaming or deleting these types would erase useful domain boundaries and create compatibility risk. The correct next step is explicit, typed translation at the points where one bounded context hands work to another, with the strategy proposal remaining non-authoritative until risk/capital authorization.

The active Futures opportunity path already has a stronger central control boundary than a simple “risk engine” diagram suggests: `TradeLifecycle.submit_opportunity` is wrapped by the capital runtime, which performs hard-limit checks, CapitalGateway authorization and reservation before calling the original lifecycle. Preserve this gate; do not add a second entry gate. The market-domain `aitos.markets.risk.RiskEngine` is a separate pure component and has not been proven to be the lifecycle authority.

## 1. Contract inventory and consumer boundaries

| Contract | Fields / semantics | Known consumers | Decision |
|---|---|---|---|
| `aitos.markets.contracts.Instrument` | Venue + asset class + symbol identity; optional tick/lot/contract/margin metadata | Market adapters, portfolio and universal-market modules | Canonical market identity. Add validation only with compatibility tests. |
| `aitos.markets.contracts.MarketEvent` | Event type, instrument, event time, payload, source, receive time, event ID, schema version, correlation ID | Market state builder, cross-market engine and ingestion adapters | Canonical market-event envelope. Keep payload schema/versioned; do not duplicate it in strategy types. |
| `aitos.markets.state.GlobalMarketState` | Regime/scores/confidence/features/as-of | Cross-market intelligence, strategy and market risk | Aggregate market context, not an event or per-instrument quote. |
| `aitos.decision.types.MarketSnapshot` | Symbol, timezone-aware timestamp, features, source versions, snapshot ID | ADL providers/runtime/replay and scanner bridge | Point-in-time decision evidence. Preserve ADL semantics. |
| `aitos.strategy.contracts.MarketSnapshot` | Instrument ID, mid, spread, volatility, funding, basis, imbalance, liquidity and numeric features | StrategyContext and built-in strategy families | Compact strategy-facing market view. Do not replace with ADL snapshot. |
| `aitos.strategy.contracts.ExecutionIntent` | Instrument ID, side, quantity, order type, limit, reduce-only, hedge group, strategy and rationale | StrategyResult and strategy built-ins | Strategy proposal; must not itself authorize an order. |
| `aitos.markets.adapters.ExecutionIntent` | Instrument, side, target quantity, slippage bound, urgency, reduce-only, reason | Universal ExecutionAdapter protocol and market package exports | Adapter boundary request. Translate only after the appropriate policy/risk checks. |
| `aitos.intelligence.autonomy_pipeline.ExecutionIntent` | Action, instrument, quantity, confidence, decision/policy IDs, risk_approved and reason | AutonomyRecord/replay serialization | Advisory pipeline output. No direct venue-dispatch consumer established in this audit. |

### Important implementation observations

1. The contracts are semantically different, not proven accidental duplicates. A future shared interface should be a protocol or explicit mapper, not a single flattened dataclass.
2. `MarketEvent` is a frozen dataclass but its `payload` is a mutable dictionary. `GlobalMarketState` is frozen but its `features` dictionary is mutable. “Frozen” therefore does not provide deep immutability. Fixing this requires an additive serialization-compatible approach and tests for existing consumers; do not silently wrap or mutate these fields in place.
3. `MarketEvent` currently assigns a correlation ID from its event ID when omitted. Event-time/receive-time validation and payload schema enforcement need a dedicated compatibility proposal.
4. ADL `MarketSnapshot` already requires an aware timestamp and exposes snapshot ID/source versions. Strategy `MarketSnapshot` is deliberately lightweight and has no equivalent point-in-time metadata. A mapper must make missing provenance explicit rather than fabricate timestamps or source versions.
5. The universal market `Portfolio` is a small in-memory view over `markets.Position`; the trading lifecycle uses a separate `PortfolioState` with equity, peak equity, positions and risk context. They are not interchangeable authorities.

## 2. Risk and capital authority map

### Confirmed active Futures opportunity path

```text
Scanner / strategy opportunity
        |
TradeLifecycle.submit_opportunity
        |
capital_runtime wrapper (installed by aitos.trading package)
        |
aitos.risk.risk_engine hard-cap check
        |
CapitalGateway.authorize_opportunity
  - point-in-time opportunity age / decay
  - circuit breaker
  - objective rank + allocation
  - PortfolioProtection (drawdown, volatility, correlation,
    funding and liquidation-buffer constraints)
  - CapitalReservation (atomic reservation)
        |
pin reservation for exchange execution
        |
original TradeLifecycle
        |
governance / execution lifecycle / durable order intent
        |
venue executor + fill/protection/account reconciliation
```

Source anchors:
- `aitos/intelligence/capital_runtime.py`: wraps the final `submit_opportunity` boundary, rejects hard-limit failures, unavailable equity, failed objective/protection/reservation and missing allocation; pins capital for exchange execution; preserves ownership on uncertain execution/open positions; releases temporary reservations when no exposure remains.
- `aitos/intelligence/capital_gateway.py`: estimates net economics, applies opportunity expiry/decay, circuit breaker, ranking/allocation, portfolio protection and reservation.
- `aitos/intelligence/capital_protection.py`: durable SQLite mode uses `BEGIN IMMEDIATE`, absolute capital/risk budgets and a reservation owner. Live paths pin reservations so a lease does not expire while exposure is unresolved.
- `aitos/trading/lifecycle.py`: contains projected-portfolio risk checks and execution lifecycle state transitions.
- `aitos/trading/__init__.py`: installs runtime guards after lifecycle class definition to avoid import cycles.

### Risk components that must not be confused

| Component | Role | Audit conclusion |
|---|---|---|
| `aitos.risk.risk_engine.RiskEngine` | Hard-cap / projected-portfolio checks in trading lifecycle | Active lifecycle check evidenced by `capital_runtime` and `TradeLifecycle` call paths. |
| `aitos.intelligence.CapitalGateway` + `PortfolioProtection` | Opportunity EV, portfolio protection, allocation and reservation | Active final opportunity authorization boundary evidenced by runtime wrapper. |
| `aitos.intelligence.CapitalReservation` | In-memory or SQLite reservation ledger; durable mode enforces aggregate absolute budgets | Active capital ownership primitive; lease/pin/restore semantics must be retained. |
| `aitos.markets.risk.RiskEngine` | Pure asset-class-neutral calculation over `markets.Portfolio` and `GlobalMarketState` | Useful market-domain primitive; not proven to be the trading lifecycle's final authority. Do not wire it in as a second veto without a design and parity tests. |

### Open risk-authority questions

- Prove whether all entrypoints—including Futures, Spot, position switching, rebalancing and any direct executor call—cross the intended central gate. The final lifecycle guard is strong, but call-site inventory must be complete.
- Correlation protection is calculated from open-position risk snapshots; reservation totals separately constrain capital and portfolio risk. Verify whether in-flight/pending candidates are included in correlation/concentration calculations before treating the projected portfolio as fully authoritative.
- `CapitalReservation` keys durable rows by uppercased symbol. Verify namespace isolation across venue, asset class, product type and account before sharing one ledger across multi-market runtimes.
- The market-domain `Portfolio` and lifecycle `PortfolioState` have different data and lifecycle semantics. A future bridge must preserve equity, mark/valuation, contract multiplier, pending orders and reservation semantics.
- Keep reduce-only paths available under bounded emergency behavior; do not accidentally make a new-entry gate block safe de-risking.

## 3. Financial record authority matrix (provisional)

This matrix records the source-backed ownership boundaries visible in the code and operational docs. It is not a claim that every writer, reader or disaster-recovery path has been enumerated.

| Aggregate | Current evidence / store | Provisional authority interpretation | Required proof / gap |
|---|---|---|---|
| Futures market-order submission | Scoped SQLite `execution_intents` journal; stable client order ID; SUBMITTING → CONFIRMED/REJECTED → APPLIED | Durable local evidence for submission/recovery, not the exchange's ultimate fill truth | Enumerate every writer; verify DB backup, multi-process ownership, account namespace and crash recovery. |
| Futures venue order/fill | Venue order lookup/client ID and execution responses | Exchange is authoritative for actual order status, executed quantity and fill price | Reconcile partial fills, child orders, fees, funding and out-of-band/manual orders. |
| Futures open/closed trade lifecycle | In-memory hydrated lifecycle plus ClickHouse write-through/history; SQLite intent acknowledgement | Lifecycle owns operational trade state; venue confirms external execution; ClickHouse archives analytics/history | Cross-store writes are ordered, not atomic. Test ClickHouse outage, crash between archive and APPLIED acknowledgement, and restore. |
| Futures capital reservation | SQLite `capital_reservations` / scoped live capital ledger and in-memory test mode | Reservation ledger owns AITOS capital claims; venue owns actual account exposure | Verify pinned reservation recovery and reconcile against exchange balances/positions before release. Never release on timeout. |
| Spot order intent and fills | Spot `InventoryLedger` SQLite, durable client order IDs, idempotent fill keys | Spot ledger owns local fill application/inventory projection; exchange remains external execution truth | Verify order status transitions, partial fill reconciliation, duplicate/out-of-order user-stream events and recovery from an exchange snapshot. |
| Spot position / cost / realized PnL | Spot inventory SQLite; commission conversion records | Local accounting projection derived from idempotent fills | Reconcile third-asset commissions, rebates, dust, transfers, deposits/withdrawals and external account activity. |
| Market-data hot transport | Redis Streams and bounded runtime queues | Transport/replay window, not a permanent financial ledger | Prove retention, reclaim, consumer idempotency and gap detection. |
| Market-data analytical history | ClickHouse persistence sinks / revisioned history | Historical/analytical query store | Prove writer completeness, revision ordering, late corrections and restart durability. |
| Model/config artifacts | Model/checkpoint files and registry metadata | Artifact bytes + immutable digest/version must identify deployed model | Verify atomic artifact publication, integrity, lineage and restore. |
| Decision attribution | Decision/funnel/reward/outcome stores | Diagnostic and research evidence; not capital authority | Prove join keys and complete attribution through reject/HOLD/exit/partial fill. |

### Critical cross-store invariant

The Futures recovery documentation explicitly describes ordered writes across SQLite and ClickHouse, not a distributed transaction. A failure between those writes must remain quarantined and be reconciled. No cleanup, retry or “repair” operation may infer that an ambiguous venue action failed, clear an intent, or release pinned capital.

## 4. Spot-specific integration boundary

The Spot runtime is separately constructed and is not imported by the Futures live runner. Its execution factory requires Spot enablement, execution enablement, dedicated credentials, and—when mainnet is selected—both configuration approval and an explicit `mainnet_approved` argument. The Spot runtime uses `SpotTradingCoordinator` with a `CapitalGateway`; a shared gateway is required when execution is enabled.

The Spot opportunity path also validates a LONG-only thesis, applies the shared AI Kernel and routes the resulting `Opportunity` through the Spot coordinator. Futures–Spot lead/lag is recorded as shadow metadata with `decision_impact: false` in the inspected path. Do not promote lead/lag to an entry authority without independent walk-forward validation and explicit policy approval.

Spot has its own SQLite inventory/order-intent ledger. This must be reconciled against the exchange user-data stream and REST snapshots; it must not be silently merged with Futures position state.

## 5. CI root cause discovered during Phase 1

The PR #260 merge-check failed before lint/tests because Black 25.12.0 found exactly three formatting-only diffs in:
- `aitos/spot/scanner.py`
- `aitos/spot/runtime.py`
- `tests/spot/test_spot_integration.py`

The Market Data Contract Audit job stopped at its formatting step for the same reason; its market-data tests and full suite were skipped. The backtest, PostgreSQL storage and isolated Redis recovery workflows completed successfully. The three Black-only formatting corrections are included in this branch; no executable logic was changed by those edits. Re-run CI and inspect the new logs before claiming the issue is resolved.

## 6. Required next implementation sequence

1. **Complete consumer/call-site graph:** use repository-wide import and call-site inventory for all contract constructors, serializers, event bus boundaries, persistence payloads, direct executor calls and Spot/Futures order paths.
2. **Add explicit mappers, not a universal mega-contract:** strategy proposal → authorized execution request; ADL snapshot ↔ strategy view only where a caller needs it; canonical event → bounded feature snapshot. Each mapper must be pure, versioned and reject missing/ambiguous identity.
3. **Deep immutability compatibility proposal:** make event payload and global-state feature maps safely immutable without breaking JSON encoding, dataclass/asdict consumers or historical persisted payloads. Add round-trip and mutation-rejection tests before wiring.
4. **Reservation namespace and in-flight projection:** test same symbol across venue/asset class/account, concurrent proposals, expired leases, pinned exposure, duplicate workers and pending-order correlation risk.
5. **Execution/accounting fault matrix:** process death before/after POST, ambiguous ACK, partial fill, protection POST/ID-write crash, store outage, manual/external order and restart reconciliation.
6. **Paper-only integration:** prove shared CapitalGateway, Spot/Futures isolation, fills/fees/funding and reconciliation with synthetic or isolated paper adapters. No live exchange order is required.
7. **Quant certification:** after implementation, run walk-forward/OOS/regime/side/cost/ablation, confidence intervals and benchmark comparison. Synthetic tests are not venue calibration.

## 7. Acceptance and rollback

- No contract is renamed or removed until every consumer, serialized payload and external boundary is enumerated.
- Additive adapters keep old readers operational; old/new round trips and deterministic replay must pass.
- No second risk engine, duplicate portfolio ledger or alternate capital reservation path is introduced.
- Any failed/ambiguous submission retains quarantine and capital ownership.
- Risk policy values and live execution behavior remain unchanged.
- Paper-only shadow integration is default-off until tests pass; mainnet execution remains separately disabled/approved.
- A rollback must restore the prior adapter/reader path without deleting journals or rewriting financial history.

## 8. Evidence not yet established

- Complete repository-wide static import graph and all dynamic plugin/monkey-patch call sites.
- Full database table writer/reader inventory and backup/restore proof.
- Runtime multi-process/concurrency/fault-injection results for this audit.
- Full Spot/Futures end-to-end paper soak.
- Production latency p50/p95/p99 and empirical venue execution calibration.

These remain NOT TESTED / NOT MEASURED until a reproducible CI/runtime evidence record is attached.
