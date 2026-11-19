# AITOS Architecture Evolution Program (AEP-1)

Date: 2026-11-19  
Baseline: `main` at the branch point used for `feat/architecture-evolution-baseline`.  
Status: Phase 0 baseline complete; Phase 1 source audit and additive Phase 2 shadow/immutability primitives implemented. Runtime activation and full acceptance gates remain open.

## Executive decision

Evolve AITOS in place. Do not rewrite the platform, add a second event broker, create a parallel feature store, or move all Python code to another language. Preserve the current Python-first design and optional Rust/PyO3 kernel. Keep Redis Streams as bounded hot transport, ClickHouse for analytical/history workloads, and existing transactional stores for their explicitly scoped journals/reservations. Evaluate storage changes only after a source-of-truth and failure-boundary audit.

The target is an event-driven, modular, multi-market quantitative platform with:
- one canonical market/event vocabulary and explicit compatibility adapters;
- point-in-time, versioned market/feature context for every decision;
- independent portfolio/risk authority before every risk-increasing execution intent;
- durable, reconciled order/position/accounting state;
- shared decision semantics across backtest, paper and live adapters;
- governed research, model rollout and human-controlled production authority;
- bounded, observable and recoverable critical paths.

This is a migration program, not permission to activate new trading behavior. No live orders, production deployment, schema migration, data reset, strategy change, risk-limit change, or execution-policy change is authorized by this document.

## Evidence labels and scope

- **SOURCE-VERIFIED** means code and/or call paths were inspected.
- **DOCUMENTED** means the repository states the capability, but its runtime path was not independently established here.
- **NOT TESTED** means no relevant test or runtime verification was performed in this tranche.
- **BLOCKED** means an overlapping branch, unavailable environment, or approval boundary prevents safe integration.

Static inspection does not prove process health, exchange correctness, durability under power loss, performance, or profitability. Latency percentiles and production capacity remain NOT MEASURED unless a separate evidence record supplies reproducible samples.

## Current architecture: verified building blocks

| Domain | Existing owner / contract | Assessment |
|---|---|---|
| Application composition | `run_paper_trading.py`, `run_live_trading.py`, `aitos/app.py` | SOURCE-VERIFIED entrypoints; runtime lifecycle still needs environment-specific evidence |
| Market-data ingress events | `aitos/market_data/contracts.py` | SOURCE-VERIFIED active bus envelope with source/ingest times, sequence, event revision, trace and schema version |
| Universal market-domain events | `aitos/markets/contracts.py` | SOURCE-VERIFIED asset-class-neutral `Instrument` and separate domain `MarketEvent`; production bridge from MarketDataBus not established |
| Cross-market context | `aitos/markets/state.py` | SOURCE-VERIFIED immutable `GlobalMarketState` and deterministic builder |
| Market ingress | `aitos/market_data/*` and canonical market-data documentation | Existing canonical pipeline; freshness and backpressure policy is documented |
| Transport/storage | Redis Streams, Clickhouse and scoped transactional stores | Existing separation is documented; each table/journal's authority and runtime writers require a complete inventory |
| Portfolio primitives | `aitos/markets/portfolio.py` | SOURCE-VERIFIED positions, gross/net notional, PnL and asset-class exposure |
| Universal risk | `aitos/markets/risk.py`; `aitos/risk/risk_engine.py` | SOURCE-VERIFIED risk components exist; authority and composition across all entry paths must be traced |
| Strategy contracts | `aitos/strategy/contracts.py` | SOURCE-VERIFIED strategy context, capital requests, intents and correlation metadata |
| Decision intelligence | `aitos/decision/types.py`, ADL runtime and replay | SOURCE-VERIFIED typed probabilistic decision contracts and replay-oriented interface |
| Execution recovery | `aitos/execution/*`, `docs/EXECUTION_RECOVERY.md` | Strong durable-intent and fail-closed design; protective-order crash recovery and venue certification remain explicit gates |
| Quant/research | `docs/QUANTITATIVE_INTELLIGENCE_ENGINE.md` and associated modules | Broad statistical/replay/validation capability; empirical calibration and complete runtime wiring are not implied |
| Learning/governance | `docs/CONTINUAL_LEARNING_ARCHITECTURE.md`, model registry and approval services | Governed lifecycle exists in part; deployed identity provider, schedules, canary collection and production adapters remain separate gates |
| Operations | health server, data-flow telemetry, CI and deployment workflows | Source instrumentation exists; live scrape, retention, complete tracing and SLO evidence need verification |

## Architectural findings and gaps

### AEP-001 — Canonical contracts have overlapping definitions

Source inspection found multiple similarly named contracts. The Phase 1 consumer/semantic audit now shows some are distinct bounded-context contracts, while the two market-event envelopes expose a real integration gap:
- `aitos/market_data/contracts.py::MarketEvent` (active canonical bus envelope);
- `aitos/markets/contracts.py::MarketEvent` (separate asset-neutral domain envelope);
- `aitos/statistics/financial_ml.py::MarketEvent` (compact research sample);
- `aitos/decision/types.py::MarketSnapshot` and `aitos/strategy/contracts.py::MarketSnapshot`;
- `aitos/strategy/contracts.py::ExecutionIntent`, `aitos/markets/adapters.py::ExecutionIntent`, and `aitos/intelligence/autonomy_pipeline.py::ExecutionIntent`.

The market-domain contract already provides `Instrument` and `MarketEvent`, while `GlobalMarketState` is a separate aggregate. This is not proof that all duplicates are currently incompatible, but it is a concrete contract-drift risk.

**Decision:** do not rename or delete these contracts in bulk. The two snapshot types and three intent types have distinct bounded-context semantics. The market-data and universal-market event envelopes are separate; the former is the live bus contract, while a pure provenance-preserving converter has now been added in `aitos/markets/bridges.py`, but production runtime wiring remains absent (the cross-market engine's only direct constructor hit was its test). An additive, instrument-resolved, provenance-preserving pure bridge now exists; do not claim universal market context is fed by the canonical bus until its read-only/shadow runtime consumer is wired and validated. The statistics event is a research sample, not a transport contract. Preserve serialized compatibility and test old/new round trips.

### AEP-002 — Market state is present, but end-to-end provenance is not yet one contract

`GlobalMarketState` is immutable and `decision.MarketSnapshot` already carries a timestamp and source-version mapping. The market-domain event also carries source, event/receive timestamps, event ID and schema version.

The remaining architecture task is to prove and standardize lineage between these existing types rather than invent another `MarketState` class. See `engineering/AEP1_PHASE1_CONTRACT_AND_AUTHORITY_AUDIT.md` for the consumer boundary and immutability observations.

**Target requirements:** stable snapshot ID; instrument identity; event-time and observation-time semantics; per-source freshness/quality; feature/model/config versions; missing/degraded-input reasons; deterministic serialization; bounded provenance references. Never place raw unbounded payloads in the hot decision object. The builder must fail closed or explicitly degrade according to a documented per-feature policy.

### AEP-003 — Portfolio and risk primitives need a single enforced authority path

The repository has market portfolio primitives, a universal market risk engine and a separate risk package. Strategy contracts also carry capital requests, positions, portfolio delta, risk budget and measured strategy correlations.

The gap is not “no portfolio/risk code”; it is a source-backed proof that every risk-increasing path reserves capital and passes through the same authoritative, concurrency-safe projected-portfolio gate.

**Target requirements:** include open positions, pending orders and in-flight reservations; calculate gross/net/concentration and correlated exposure; apply asset-specific margin/contract semantics; reserve atomically before dispatch; keep reduce-only de-risking available under explicitly bounded emergency policy; make risk decisions immutable and auditable. Do not add a new optimizer until existing allocation and lifecycle paths are mapped and tested.

### AEP-004 — Financial truth is distributed across purpose-specific stores

Existing documentation describes ClickHouse analytical/history, Redis hot state/transport, SQLite execution/capital/revision journals, model/checkpoint files and a PostgreSQL fallback/capability path. These stores do not share one cross-database transaction.

**Decision:** do not introduce another database or silently declare ClickHouse the authority for every financial record. Publish a per-aggregate authority matrix (orders, fills, positions, cash, fees, funding, reservations, model artifacts, market history), transaction boundary, idempotency key, reconciliation source and recovery procedure. Only after that audit decide whether an OLTP consolidation is justified. Reconciliation must detect discrepancies; it must not silently repair or release capital.

### AEP-005 — Research/live parity is a contract and test problem

Backtest, paper and live have different execution adapters and unavoidable fill-model differences. They must share strategy evaluation, decision schema, risk semantics and lifecycle intent contracts, while each adapter reports its actual execution assumptions and evidence.

**Target requirements:** point-in-time feature availability; deterministic replay; immutable dataset/code/config/model digests; event-time ordering; realistic fees, spread, funding, latency, partial fills, queue and market impact; explicit simulator-vs-venue differences; decision/rejection/HOLD/exit attribution. Synthetic tests are not empirical venue calibration.

### AEP-006 — Execution recovery still has explicit unresolved crash windows

`docs/EXECUTION_RECOVERY.md` explicitly records that market-order durable intent does not yet constitute a complete protective-order intent ledger: a crash between protective POST and ID persistence requires open-algo-order inventory/reconciliation. It also documents adapter-specific capability limitations and the need for complete account/position/protection recovery tests.

**Decision:** retain fail-closed quarantine and no-blind-resubmit behavior. Build a fault matrix and adapter capability contract before modifying the active order lifecycle. No live activation is part of AEP-1.

### AEP-007 — Model governance primitives are not the same as a deployed governance service

The repository includes validation, registry, approval and rollout primitives. Their presence does not prove authenticated identity middleware, durable scheduled evaluation, canary evidence collection, alert delivery, rollback adapters or production operation are deployed.

**Target requirements:** immutable artifact/data/config provenance; explicit role-bound approval; challenger shadow; evidence thresholds; controlled canary; automatic *halt/rollback only under pre-authorized deterministic conditions*; human approval for promotion; append-only audit.

### AEP-008 — Observability needs end-to-end coverage evidence

Existing data-flow and lifecycle telemetry is valuable. Complete stage correlation, durable decision traces, bounded cardinality, retention, alert delivery and real scrape/render verification remain acceptance items. Existing latency baseline correctly marks production percentiles NOT MEASURED; preserve that truth.

## Target logical architecture

```text
External venues / providers / macro / news / on-chain
                     |
          Adapter + instrument master
                     |
       Normalize / validate / sequence / freshness
                     |
         Canonical event contracts + lineage
              /             |             \
       bounded hot state   event bus    durable history
              \             |             /
                point-in-time feature context
                             |
                Global / per-instrument state
                             |
             independent strategy proposals
                             |
             decision + EV + uncertainty + rank
                             |
          portfolio projection + atomic reservation
                             |
                 independent risk authority
                             |
             durable OMS/EMS execution intent
                             |
                venue-specific execution adapter
                             |
            fills + accounting + reconciliation
                             |
             decision/outcome attribution store
                             |
              research + governed model lifecycle
                             |
             shadow/canary + operator approval
```

Control plane (configuration, identity, secrets, observability, CI/CD, incident response and operator console) spans all layers but must not bypass their authority boundaries. Background training, graph queries, RAG, historical backfill and reporting must remain outside the latency-sensitive market/execution path.

## Delivery plan and dependency order

### Phase 0 — Baseline and overlap coordination (this tranche)
- Record current source-backed components and gaps.
- Identify overlapping open work before editing shared domains.
- Establish non-negotiable invariants, status vocabulary and acceptance gates.
- No runtime behavior changes.

**Exit:** this baseline document, architecture map pointer, branch/PR handoff and explicit blockers.

### Phase 1 — Contract and ownership audit (source-level audit completed; runtime gates remain)
- Generate module/import/dependency inventory from source.
- Map every `MarketSnapshot`, `ExecutionIntent`, `MarketEvent`, portfolio and risk contract and all serialization/persistence consumers.
- Validate the new pure canonical MarketDataBus → universal market-domain converter. An opt-in bounded read-only `MarketDataShadowConsumer` class now exists with explicit instrument resolution, freshness, dedupe, overload accounting and preserved sequence/revision/trace lineage; it is deliberately not wired into an application entrypoint because a single authoritative multi-market instrument resolver has not been proven.
- Publish data authority matrix and per-aggregate recovery ownership.
- Trace every entry/exit/reduce-only path to the authoritative risk and execution gates.
- Coordinate with open PR #246 and #250; do not duplicate or rebase their work without review.

**Exit:** semantic inventory and provisional authority matrix recorded in `engineering/AEP1_PHASE1_CONTRACT_AND_AUTHORITY_AUDIT.md`; pure bridge and shadow-consumer unit coverage added. Runtime writer coverage, full call-site graph, authoritative instrument-master integration and paper fault matrix remain open.

### Phase 2 — Canonical market context (approval required before active wiring)
- Extend existing contracts only where the inventory demonstrates missing fields.
- Add stable snapshot/version/lineage references and explicit quality/freshness states.
- Add deterministic serialization, compatibility tests and replay fixtures.
- Deeply immutable JSON-compatible event/state mappings now exist for canonical and market-domain event payloads, stage timestamps and GlobalMarketState features; JSON/asdict and mutation tests are added.
- Shadow-compare new context against existing consumers before cutover.

**Exit:** old/new parity, stale/degraded handling tests, replay reproducibility and explicit operator approval for cutover.

### Phase 3 — Portfolio/risk authority (approval required before active wiring)
- Added a typed `PendingOrderExposure` contract to the existing `PortfolioState`; duplicate pending client identities, invalid sides and non-finite/negative risk inputs are rejected.
- The final lifecycle capital guard now projects each pending order as a distinct exposure key while retaining its underlying instrument identity and LONG/SHORT side. This means an already-open BTC position and another pending BTC order are both counted, with same-instrument correlation and side-aware hedge treatment; they are no longer collapsed by a symbol-keyed dictionary.
- The existing RiskEngine also includes pending notional in gross/sector exposure, pending leverage in the leverage ceiling, and the union of open/pending symbols in max-open-position checks. Existing open + pending orders for one symbol count as one position slot but their notionals aggregate for concentration; additional distinct symbols consume additional slots.
- The pending-order projection is additive and defaults to empty for existing providers. The pure Binance USD-M adapter is implemented and tested, but no strategy stop resolver has been certified for arbitrary exchange-resting entry orders. The live runner therefore performs a full authenticated position/open-order reconciliation before startup and every new-entry scan; it fails closed when an unpriced entry order is present. Automatic numeric pending-risk projection into the live PortfolioState remains gated on an authoritative stop-risk provider.
- Trace and consolidate existing allocation/risk path; do not create parallel risk engines.
- Add pending-order and in-flight reservation projection if absent.
- Verify atomic reservation under concurrent proposals.
- Prove risk veto cannot be overridden and reduce-only exits remain safely bounded.

**Exit:** property/concurrency/fault tests and paper-only integration evidence; no change to risk policy values without separate approval.

### Phase 4 — Financial/execution integrity (approval required before active wiring)
- Map order/fill/position/cash/fee/funding authority.
- Complete protective-order intent inventory and restart reconciliation.
- Implemented a read-only full-account audit: local open trades are compared with the full venue position snapshot; ordinary open orders are classified conservatively; unpriced entry orders block the live scan. The audit is run at startup and before every live scan.
- Durable protective intents now retain terminal venue state (`CANCELED`, `FILLED`, `EXPIRED`, etc.) and executed quantity. Confirmed terminal cancellation updates the scoped journal, so a replaced stop is not falsely expected to remain in the open-algo inventory. Closed-trade protection history is filtered by active trade ownership; an orphaned resting algo order remains a blocking finding.
- Verify duplicate/out-of-order/partial fills, ambiguous ACK, process death, disk-full and store outage.
- Define backup/restore and operator reconciliation procedures.

**Exit:** isolated adapter and Redis/ClickHouse/transactional-store fault matrix; no unresolved state is silently treated as safe.

### Phase 5 — Research-to-live parity and quant certification
- Reuse canonical decision and risk contracts across backtest/paper/live adapters.
- Complete data/model/feature provenance and execution calibration.
- Run walk-forward/OOS/regime/side/cost/ablation and multiple-testing controls.
- Publish confidence intervals and benchmark-relative results.

**Exit:** reproducible evidence package; no profitability claims from synthetic fixtures.

### Phase 6 — Governance and operations
- Verify real identity middleware, alert sinks, scheduled drift evaluation and canary collectors.
- Complete end-to-end traces, retention, SLO definitions, capacity and disaster-recovery drills.
- Keep candidate promotion human-approved and live trading disabled absent separate authorization.

**Exit:** independent review and operational acceptance record.

## Explicit non-goals / decisions deferred

- No blanket microservices conversion.
- No Kafka/Redpanda/Flink/Ray adoption without workload benchmark and operational justification.
- No blanket Rust/C++/Go rewrite; Python remains primary and optional Rust remains benchmark-gated.
- No second feature store, second risk engine or duplicate portfolio ledger.
- No automatic model promotion or autonomous risk-policy mutation.
- No VPS deployment, live trading, real exchange orders, data deletion/reset or destructive migration.
- No production latency or profitability claims without measured evidence.

## Approval and rollback gates

Architecture-sensitive changes to event schemas, subscriptions, Redis consumer semantics, persistence/recovery, concurrency, queue limits, portfolio/risk authority or order lifecycle require a concrete change proposal, dependency analysis, regression tests, rollback plan and explicit approval before activation/deployment.

Every migration must be additive first, support old readers during transition, include deterministic backfill/reconciliation where needed, and have a tested rollback. Never drop old fields/tables or release uncertain reservations as part of a convenience cleanup.

## Current blockers and coordination

- **PR #246** (quantitative intelligence foundations) and **PR #250** (on-demand historical market intelligence) were confirmed merged before this Phase 1 audit. Their current main-branch implementations are treated as existing platform capabilities; do not recreate their modules or overwrite their accepted behavior.
- PR #244/#215 concern Binance testnet routing; testnet routing is not equivalent to simulated paper or live readiness.
- Runtime access and current local process/position state were not available in this GitHub-only tranche. Runtime, exchange, service-backed and latency checks are NOT TESTED / NOT MEASURED.

## Verification record for this tranche

- Repository instructions `AGENTS.md`, `skills/aitos-skill.md` and `engineering/AGENT_OPERATING_MODEL.md` reviewed.
- Architecture, market-data, multi-market, freshness, compute-aware intelligence, execution recovery, quantitative intelligence, module inventory, latency baseline and backend-first roadmap reviewed.
- Source contracts inspected: `aitos/markets/contracts.py`, `aitos/markets/state.py`, `aitos/markets/portfolio.py`, `aitos/markets/risk.py`, `aitos/decision/types.py`, `aitos/strategy/contracts.py`.
- Phase 1 semantic contract and provisional financial-authority audit: `engineering/AEP1_PHASE1_CONTRACT_AND_AUTHORITY_AUDIT.md`.
- CI was run for the initial docs head; formatting failed on three unrelated Spot files, subsequently corrected with formatting-only changes in this branch. Latest CI must be rechecked before claiming pass. Runtime checks and production latency remain NOT TESTED / NOT MEASURED.


## Additional Phase 3/4 implementation — pending exposure and protection inventory

The branch now contains a pure Binance USD-M open-order projection adapter at `aitos/exchange/binance_pending_exposure.py`. It uses only remaining quantity after partial fills, classifies one-way and hedge-mode entry/closing sides, excludes reduce-only/close-position/hedge-closing orders, and requires mark price, protective stop and leverage inputs. Both the open-order snapshot and risk-input snapshot must be timezone-aware, no older than five seconds, and within two seconds of each other. The adapter is linear USD-M only; inverse/coin-margined contracts need a separate valuation adapter. Missing or incoherent inputs fail closed. It returns a new `PortfolioState`; it is not silently wired to a runtime provider because the authoritative account snapshot lifecycle owner is not yet certified.

`BinanceFuturesOrderExecutor` now exposes authenticated read-only `get_open_orders` and `get_open_algo_orders` inventory readers. `aitos/execution/protection_reconciliation.py` joins the scoped local intent journal with authenticated algo inventory, full account positions and ordinary open orders. The live runner invokes the fail-closed guard at startup and before each new-entry scan. The guard rejects local/venue position mismatches, unpriced entry orders, active local positions without a stop ID, stops missing from authenticated algo inventory, orphaned protective orders, and protective intent/order mismatches. It never auto-repairs or resumes from ambiguous evidence. Terminal protection state and fill quantity are written only after venue terminal evidence is confirmed. This is still not a complete exchange-wide financial ledger or automated POST-to-ACK resolution protocol.

Regression coverage includes partial fills, hedge-mode closing legs, reduce-only exclusion, stale/incoherent snapshots, missing stop/leverage/mark inputs, duplicate identities, malformed exchange inventory, scope mismatch, position mismatches, unpriced pending entries, closed-trade protection ownership, terminal protection journal transitions and the invariant that a matching open protection order cannot authorize resume.
