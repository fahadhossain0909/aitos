# AEP-1 Phase 1 — Contract, Risk and Financial Authority Audit

Date: 2026-11-19
Status: source-level audit; no runtime or production certification.
Baseline inspected: current `main` source as of this audit; PR #260 carries this audit forward on its feature branch.

## Executive decision

**Do not collapse the similarly named contracts into one Python class.** Source inspection shows two patterns: some same-named objects belong to different bounded contexts and carry different semantics; the market-data plane and universal-markets domain also contain separate event envelopes that need an explicit bridge:

- `market_data.contracts.MarketEvent` is the actual canonical market-data bus envelope used by adapters, Redis/EventBus transport, scanner, Spot and persistence consumers.
- `markets.contracts.MarketEvent` is a separate asset-class-neutral domain envelope with an explicit `Instrument`; no production runtime consumer from the canonical market-data bus to this contract was established in the audit; a pure, explicit converter has now been added.
- `statistics.financial_ml.MarketEvent` is a compact event-time research sample (timestamp/price/volume/signed volume), not a transport envelope.
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
| `aitos.market_data.contracts.MarketEvent` | Exchange/market/symbol, event/ingest times, source, event ID, schema/revision, sequence, trace/correlation and payload | MarketDataBus, canonical adapters, live scanner, Spot consumers, persistence/deep-orderbook sinks | Actual canonical ingress/bus envelope. Keep this as transport contract. |
| `aitos.markets.contracts.MarketEvent` | Event type + explicit `Instrument`, event/receive times, payload, source, event/schema/correlation IDs | `markets.engine.CrossMarketIntelligenceEngine` and universal-market tests | Separate downstream domain envelope. No production bridge/consumer from MarketDataBus was established; do not call it the live bus canonical event. |
| `aitos.statistics.financial_ml.MarketEvent` | Timestamp, price, volume, signed volume, optional event ID | FinancialMLPipeline and event/volume/dollar/imbalance bar builders | Research sample, not a market-data bus contract. Translate only after side/quantity semantics are validated. |
| `aitos.markets.state.GlobalMarketState` | Regime/scores/confidence/features/as-of | Cross-market intelligence, strategy and market risk | Aggregate market context, not an event or per-instrument quote. |
| `aitos.decision.types.MarketSnapshot` | Symbol, timezone-aware timestamp, features, source versions, snapshot ID | ADL providers/runtime/replay and scanner bridge | Point-in-time decision evidence. Preserve ADL semantics. |
| `aitos.strategy.contracts.MarketSnapshot` | Instrument ID, mid, spread, volatility, funding, basis, imbalance, liquidity and numeric features | StrategyContext and built-in strategy families | Compact strategy-facing market view. Do not replace with ADL snapshot. |
| `aitos.strategy.contracts.ExecutionIntent` | Instrument ID, side, quantity, order type, limit, reduce-only, hedge group, strategy and rationale | StrategyResult and strategy built-ins | Strategy proposal; must not itself authorize an order. |
| `aitos.markets.adapters.ExecutionIntent` | Instrument, side, target quantity, slippage bound, urgency, reduce-only, reason | Universal ExecutionAdapter protocol and market package exports | Adapter boundary request. Translate only after the appropriate policy/risk checks. |
| `aitos.intelligence.autonomy_pipeline.ExecutionIntent` | Action, instrument, quantity, confidence, decision/policy IDs, risk_approved and reason | AutonomyRecord/replay serialization | Advisory pipeline output. No direct venue-dispatch consumer established in this audit. |

### Important implementation observations

1. The two MarketSnapshot types and three ExecutionIntent types are semantically different, not proven accidental duplicates. A future shared interface should be a protocol or explicit mapper, not a single flattened dataclass.
2. There are multiple MarketEvent types. The market_data contract is the active live bus envelope; the markets contract is a separate domain projection; the statistics contract is a compact research sample. The current code search did not establish a production adapter from MarketDataBus to CrossMarketIntelligenceEngine, and the engine's only direct constructor hit was its universal-market test. This is a real runtime integration/provenance gap, not a reason to replace the live bus contract. The new pure converter is additive and deliberately not wired into a production loop yet.
3. Both market event envelopes are frozen dataclasses but their `payload` dictionaries are mutable. `market_data.MarketEvent.stage_timestamps` and `GlobalMarketState.features` are mutable dictionaries too. “Frozen” therefore does not provide deep immutability. Fixing this requires an additive serialization-compatible approach and tests for existing consumers; do not silently wrap or mutate these fields in place.
4. The universal-markets event currently assigns a correlation ID from its event ID when omitted. Event-time/receive-time validation and payload schema enforcement need a dedicated compatibility proposal.
5. ADL `MarketSnapshot` already requires an aware timestamp and exposes snapshot ID/source versions. Strategy `MarketSnapshot` is deliberately lightweight and has no equivalent point-in-time metadata. A mapper must make missing provenance explicit rather than fabricate timestamps or source versions.
6. `StrategyEngine` deliberately stops before the risk gate and execution adapter; repository docs explicitly state built-in strategies are intent generators and are not wired to live execution. Repository search found no production consumer of `StrategyResult.intents` and no call to the universal `ExecutionAdapter.submit`. The strategy proposal and adapter request are separate planned stages, not a broken active dispatch path.
7. The universal market `Portfolio` is a small in-memory view over `markets.Position`; the trading lifecycle uses a separate `PortfolioState` with equity, peak equity, positions and risk context. They are not interchangeable authorities.
8. The additive market-data bridge now requires an explicit `Instrument.market_type` and checks it against the source event. When provided, the instrument ID includes venue + asset class + market type + symbol, preventing same-symbol Spot/Futures identity collisions. Legacy instruments without market_type retain their old ID shape; the bridge rejects them rather than guessing.

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

### Entry/exit and order-dispatch path map

| Path | Observed boundary | Risk / capital treatment | Residual proof |
|---|---|---|---|
| Futures entry from app | `aitos.app` calls `TradeLifecycle.submit_opportunity` | `capital_runtime` wraps the final lifecycle method; hard risk check, CapitalGateway, PortfolioProtection and reservation run before the original lifecycle submits | Exercise every alternate app/worker/plugin invocation and concurrent duplicate opportunity IDs |
| Futures scanner / position switch replacement | Scanner and `PositionSwitchCoordinator` call `TradeLifecycle.submit_opportunity` | Replacement still traverses the same wrapped lifecycle boundary; switching coordinator is paper/test-only and refuses production opportunities | Verify recovery worker only starts after persisted trades and venue positions are hydrated/reconciled |
| Futures close / partial close | Lifecycle close/reduction methods call the configured order executor; switch close uses `OrderExecutorCloseAdapter` | Reduce-only close is a separate de-risking path, bound to original venue/position and requires authoritative fill/flat evidence before local close or reservation release | Full multi-process partial-fill and venue-account reconciliation remains untested |
| Spot entry | `SpotRuntime.submit_opportunity` → `SpotTradingCoordinator.submit_entry` | LONG-only; shared CapitalGateway; namespaced Spot InstrumentKey reservation; durable inventory intent; reservation pinned before network submission | Verify account-scoped namespace and reconciliation across simultaneous Spot/Futures workers |
| Spot exit | `SpotTradingCoordinator.submit_exit` | Requires EIE EXIT/MANAGE, LONG inventory bound and unresolved-order veto; sell quantity cannot exceed managed inventory; no venue reduceOnly flag exists for Spot | Verify manual/external account inventory and commission/dust reconciliation; Spot sells remain limited to AITOS-managed inventory |
| Direct venue adapters | Binance Futures executor, Spot adapter, routed executor and broker adapters | Venue adapters are below the lifecycle/coordinator boundaries; unsupported broker reductions fail closed; live routed reductions require original-venue binding | Complete static search including dynamic factories/monkey patches and confirm no production caller bypasses the coordinators |

The current code search found the application and switch replacement entrypoints above,
and the Spot runtime/coordinator entrypoints. It did not establish a separate
production `execute_rebalance` order-dispatch path. Do not infer that a missing
search hit proves the absence of dynamically loaded/plugin paths; keep the full
runtime call graph as an open verification item.

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
- `CapitalReservation` keys durable rows by uppercased string. Spot currently passes a namespaced `InstrumentKey`, while Futures paths commonly use a symbol. Verify namespace isolation across venue, asset class, product type and account before sharing one ledger across multi-market runtimes.
- The market-domain `Portfolio` and lifecycle `PortfolioState` have different data and lifecycle semantics. A future bridge must preserve equity, mark/valuation, contract multiplier, pending orders and reservation semantics.
- Keep the StrategyEngine no-execution boundary. No production consumer of `StrategyResult.intents` or universal `ExecutionAdapter.submit` was found; a future rollout must cross the shared capital/risk gate and begin in paper/shadow mode.
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

1. **Complete consumer/call-site graph:** use repository-wide import and call-site inventory for all contract constructors, serializers, event bus boundaries, persistence payloads, direct executor calls and Spot/Futures order paths. The additive pure converter `aitos.markets.market_data_event_to_domain` preserves sequence/revision/trace provenance. A bounded, opt-in, live-only `MarketDataShadowConsumer` now exists in `aitos/markets/shadow_consumer.py`; it has an explicit resolver callback, market-type allow-list, bounded queue, stale/out-of-order/dedupe/overload counters and no execution authority. It is not wired into an application entrypoint until the authoritative multi-market instrument resolver and lifecycle ownership are established.
2. **Add explicit mappers, not a universal mega-contract:** canonical event → bounded market-domain event now has a pure additive converter. Do not map strategy proposal → adapter request until the StrategyEngine rollout is actually authorized; when that stage is implemented, the mapper must cross the shared capital/risk authority rather than call the venue directly. ADL snapshot ↔ strategy view only where a caller needs it. Each mapper must be pure, versioned and reject missing/ambiguous identity.
3. **Deep immutability:** implemented recursive JSON-compatible `FrozenDict`/`FrozenList` for canonical market-data event payloads and stage timestamps, universal market-domain event payloads and stage timestamps, and `GlobalMarketState.features`. Added nested mutation rejection, JSON encoding and dataclass/asdict regression tests. Historical wire shape remains dict/list; old persisted JSON deserializes through the same contract constructors.
4. **Reservation namespace and in-flight projection:** the CapitalGateway now includes active reservations for symbols not represented by the open-position risk snapshot in the correlation-risk projection; unknown side metadata receives conservative unknown-correlation treatment. The same projection is re-evaluated inside the SQLite `BEGIN IMMEDIATE` reservation transaction before insert, preventing two durable workers from independently approving stale correlated-risk snapshots. Added in-flight, no-double-count and cross-instance atomicity tests. Remaining: test same symbol across venue/asset class/account, concurrent proposals, expired leases, pinned exposure, duplicate workers, and same-symbol open-position plus additional pending-order risk. Extend market-scoped identity tests to Spot and USD-M/COIN-M Futures sharing the same ticker symbol.
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


## 9. Phase 2 implementation addendum

The follow-on implementation and safety semantics are documented in
`engineering/AEP1_PHASE2_SHADOW_AND_IMMUTABILITY.md`.

- `aitos/core/immutable.py`: JSON-compatible recursive immutable mappings/lists.
- `aitos/markets/shadow_consumer.py`: opt-in bounded read-only MarketDataBus consumer.
- `aitos/intelligence/capital_gateway.py`: unrepresented active reservations are included in projected correlation risk.
- `tests/core/test_immutable.py` and `tests/markets/test_shadow_consumer.py`: immutability, serialization, queue/backpressure, stale input, dedupe, instrument identity and lifecycle tests.
- `tests/test_capital_runtime.py`: pending reservation projection regression.

The shadow consumer is deliberately not started by any app entrypoint. Its queue
is lossy/at-most-once after the bus callback returns and is not a durable consumer.
The generic CrossMarketIntelligenceEngine's arrival-ordered samples are not
causal lead-lag evidence; the existing timestamp-bucketed FuturesSpot analyzer
remains the correct causal diagnostic.

## 10. Runtime integration update — 2026-11-19

The authoritative instrument resolver groundwork is now present as the immutable, versioned `InstrumentMaster` (`aitos/markets/instrument_master.py`). It requires explicit venue, market type and symbol identity; it does not infer aliases or cross-market equivalence. The opt-in `MarketDataShadowConsumer` is now injectable through `build_system` and is started/stopped under `initialize_all` / `shutdown_all` ownership. This is a composition-root integration only: standard entrypoints remain default-off and no shadow consumer is automatically instantiated.

See `engineering/AEP1_PHASE3_RUNTIME_INTEGRATION_ADDENDUM.md` for candidate identity telemetry, candidate-scoped canary validation and current acceptance gates.
