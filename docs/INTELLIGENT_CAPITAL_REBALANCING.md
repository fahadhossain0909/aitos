# Intelligent Capital Allocation & Dynamic Trade Rebalancing

## Current-main audit

The current `main` branch already contains a venue-neutral capital objective, opportunity ranking, batch allocation, a final `TradeLifecycle.submit_opportunity` capital guard, portfolio protection, reservation accounting, loss circuit breakers, fee/slippage/funding inputs, and post-trade feedback. These components remain authoritative; this feature must not create a competing entry gate.

The existing position-management stack also has Market State, Trade Thesis, Path Planner, Structural Risk, Exit Intelligence, trade-journey evaluation, and completed-bar-aware stagnation observations. Those components remain authoritative for deciding whether an incumbent thesis is invalidated or momentum has weakened.

The missing capability identified by this feature audit is a dedicated, fee-aware comparison between an incumbent position's remaining expected edge and a replacement candidate's expected net edge, with explicit anti-churn and portfolio-risk vetoes.

## Switching contract

`aitos.intelligence.capital_rebalancing.DynamicRebalancer` is a pure policy evaluator. It accepts:

- the incumbent position's forward-looking remaining net edge, hold duration, stagnation score, thesis health and estimated exit costs;
- a replacement candidate's expected gross return, loss probability/severity, all-in entry/exit costs, confidence, liquidity, risk and liquidation buffer;
- current portfolio risk, correlated incremental risk, drawdown, available capital and recent switch activity.

It returns an auditable `RebalanceDecision` with a boolean proposal, net advantage and deterministic rejection reasons. It does not execute trades.

## Decision economics

Candidate expected net edge:

`gross return - (loss probability × expected loss) - fees - slippage - funding`

Switch advantage:

`candidate expected net edge - incumbent remaining net edge - incumbent exit fee/slippage`

The candidate's fee/slippage/funding inputs must represent its complete expected round trip. Incumbent exit costs are then added because switching forces an otherwise-unnecessary exit.

## Hard guards

- Candidate confidence must meet the strict configured threshold (default 0.85).
- The incumbent must be stagnant or have a degraded/invalidated thesis; a healthy trade is not displaced.
- The cost-adjusted advantage must exceed the configured minimum (default 0.15 percentage points).
- Minimum hold time, switch cooldown and hourly switch count prevent churn.
- Liquidity, liquidation buffer, candidate trade risk, aggregate/correlated portfolio risk, drawdown and available capital are independent vetoes.
- Missing or invalid required inputs must be treated as unavailable by the runtime adapter; do not synthesize optimistic values.

## Runtime integration added in this PR

The pure policy still never sends orders. Runtime orchestration is provided by `aitos.trading.position_switching.PositionSwitchCoordinator`, attached through `TradeLifecycle.configure_position_switching()`. `build_system()` exposes an explicit `position_switching_factory` hook so deployments can assemble the persistent journal, venue close adapter, fresh evidence callback and cost registry after the lifecycle/market context are built. The hook is unset by default.

- The scanner now gathers the full scan-cycle batch, kernel-validates opportunities, and asks the lifecycle coordinator to compare every approved candidate against every open incumbent. The injected pair-evidence callback must use current PositionManager/thesis/stagnation state and venue cost quotes. The coordinator ranks approved decisions by net advantage; only the top switch is attempted in one scan cycle.
- The coordinator journals each switch in SQLite using a persistent path and a deterministic switch identity. The database must live on durable storage and be included in backup/recovery.
- `OrderRequest.reduce_only` is now explicit. The Binance executor maps it to the venue's one-way `reduceOnly=true` or hedge-mode `positionSide` semantics. Simulated orders retain their idempotency/status records for tests.
- `OrderExecutorCloseAdapter` uses a stable client order id, reconciles by order id/client id before resubmission, requires an authoritative FILLED status, checks the full remaining quantity, requires a fresh positive reference-price provider, and requires a venue-position-flat callback. Accepted/NEW/PARTIALLY_FILLED orders are not close confirmation.
- Only after that confirmation does the coordinator call the local lifecycle close, release the incumbent reservation from the same CapitalGateway instance used by the final submit guard, refresh portfolio state, and call `TradeLifecycle.submit_opportunity()` for the replacement. The existing capital guard remains the only candidate authorization path.
- If the close is incomplete, no reservation is released and no replacement is submitted. If a crash occurs during close, retrying the same switch intent uses the stable client order id to reconcile rather than blindly resubmit. A second active switch for the same incumbent is blocked by a durable unique index. If a crash occurs after the replacement enters its submission window and the result cannot be found in lifecycle records, the coordinator fails closed and requires operator reconciliation instead of blindly submitting a duplicate. The opt-in PositionSwitchRecoveryWorker reconstructs pending candidates from the durable snapshot and resumes safe states. Enable it only after persisted trades and venue positions have been hydrated and reconciled; missing readiness gates cause a fail-closed startup error.
- Production opportunities are explicitly refused by the automatic switch coordinator. This feature is for controlled paper/test environments only.

`aitos.trading.rebalance_evidence.PositionManagerEvidenceAdapter` evaluates the incumbent through the existing PositionManager and exposes thesis consistency, journey health/path adherence, completed-bar stagnation observations, and an explicitly calibrated remaining-edge estimator. The remaining-edge estimator is mandatory because Exit Intelligence's heuristic score is not itself a return percentage. The pair-evidence callback must attach the fresh quote's `as_candidate_costs()` mapping to `Opportunity.agent_consensus['execution_costs']`; the final `capital_runtime` guard now forwards those fee/slippage/funding values into `CapitalGateway.authorize_opportunity()` rather than reverting to generic defaults. The evidence callback and venue adapters are deliberately injected rather than replaced with guessed values. A missing callback means no switch decisions; a missing venue-flat confirmation means no close confirmation.

## Venue execution-cost intelligence

`aitos.execution.cost_intelligence.VenueExecutionCostRegistry` requires a registered venue adapter and validates venue/symbol/side identity plus quote freshness. `BinanceUsdMExecutionCostAdapter` provides the quote contract. `BinanceUsdMAccountCostProviders` now wires it to the authenticated executor's `/fapi/v1/commissionRate`, `/fapi/v1/premiumIndex`, `/fapi/v1/fundingInfo` and `/fapi/v1/depth` endpoints. It computes round-trip depth impact by sweeping both entry and exit sides of the book and fails closed when displayed depth is insufficient. The actual liquidation-distance calculation remains an injected account-risk provider: it must calculate the proposed position against the account's real margin mode, wallet balance and maintenance-margin bracket; a current unrelated position's liquidation distance must not be reused. Positive funding is charged to LONG and credited to SHORT; negative funding reverses that direction. This provider wiring has unit coverage but is not verified against a live/testnet account. Quotes include round-trip taker fees, expected round-trip slippage/market impact, signed funding cost over the requested holding horizon, and liquidation distance for the actual leverage/margin mode. Funding credits are represented as negative costs; missing/stale quotes must veto a switch. No universal fee or liquidation-distance estimate is substituted.

## Rollout and validation

- Unit-test the pure decision policy and all vetoes.
- Keep coordinator attachment opt-in; it is not automatically installed during application startup. Do not configure it for production.
- The code now includes a versioned durable candidate snapshot, opt-in startup recovery worker, PositionManager evidence adapter, authenticated Binance commission/funding/depth provider wiring, cross-process SQLite contention tests, a deterministic normalized lifecycle-event JSONL replay runner with durable checkpoints, and a paired hold-only versus rebalancing backtest harness. Tests cover candidate reconstruction/restart resume, missing-payload fail-closed, partial/cancelled close no-open behavior, partial-close retry without a fresh alpha veto, signed funding, quote freshness, startup retry semantics and cross-process journal contention. Actual exchange-event capture/source integration, liquidation-provider verification, live/testnet adapter verification and full historical evaluation remain required.
- Feed point-in-time frames from the ClickHouse historical source into aitos.backtest.capital_rebalance.CapitalRebalanceReplay; the harness reports paired hold-only/rebalanced equity, drawdown, switch count, transaction costs and incremental return. The harness does not generate historical features or claim results by itself. Run it on captured data with realistic fees, spread, depth impact, funding, liquidation assumptions and walk-forward partitions before interpreting the comparison.
- Run paper trading and restart recovery before any production enablement.
- Never relax existing drawdown, liquidation, position-size, portfolio-risk or circuit-breaker controls to increase trade frequency.

This document and pure evaluator do not claim profitability improvement. The coordinator, versioned SQLite journal, candidate reconstruction, opt-in startup recovery worker, PositionManager evidence adapter, Binance USD-M cost adapter contract, reduce-only request contract, scan-cycle batch hook and venue quote registry are implemented. Deployment wiring of authenticated Binance providers, calibrated remaining-edge estimator, persistent journal path and exchange position-flat query remains outstanding. Real-time exchange verification, exhaustive failure injection, multi-process contention, historical replay and paper-runtime verification are intentionally deferred to the later combined runtime-test phase. No profitability claim is made.


## Cross-process journal guarantees

`SQLiteSwitchJournal` serializes writes with `BEGIN IMMEDIATE` and enforces one active intent per incumbent through a partial unique index. A process-level regression test starts independent processes with independent journal connections and races distinct switch IDs for the same incumbent; exactly one intent may become active. This is a database-level contention test, not a distributed lock for a network filesystem. Keep the journal on a local durable filesystem with SQLite locking semantics; do not place it on NFS or a shared volume whose locking guarantees are unknown.

## Explicit Binance provider construction

A deployment can construct the authenticated market/account cost providers with `BinanceUsdMAccountCostProviders(executor, liquidation_distance_provider=...)` and call `build_adapter()`. The `executor` must be an authenticated Binance USD-M executor connected to the intended account. The liquidation callback is mandatory and must fail closed when it cannot calculate a reliable proposed-position liquidation buffer. API credentials and account-specific values are never embedded in AITOS source code.


## Replay and comparative evaluation code

- aitos.backtest.lifecycle_event_replay.LifecycleEventReplay replays normalized captured order/account lifecycle events from JSONL in timestamp order. A SQLite checkpoint suppresses already projected event IDs across process restarts. The projector must be idempotent because a process can crash after applying an event but before checkpoint commit. This harness does not submit exchange orders.
- aitos.backtest.capital_rebalance.CapitalRebalanceReplay runs paired hold-only and rebalanced paths over the same timestamp-ordered frames. Each frame keeps ex-ante IncumbentPosition/RebalanceCandidate/RebalanceContext separate from next-period realized returns to avoid look-ahead. It rejects frames whose incumbent evidence does not match the currently active rebalanced symbol. Actual ClickHouse event-to-feature projection, account/order lifecycle event persistence, and a full point-in-time dataset still need to be supplied before a valid historical result can be produced.
- An incomplete close after a partial fill is resumed from its durable close intent even if a newly evaluated alpha decision would now veto the switch. The system must finish reconciling the already-reduced position; it cannot pretend the partial close never happened. A canceled partial order that leaves residual exchange quantity still requires venue-specific residual-order reconciliation and must not release the reservation or open a replacement.
