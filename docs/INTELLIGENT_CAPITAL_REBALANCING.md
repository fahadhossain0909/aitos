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
- If the close is incomplete, no reservation is released and no replacement is submitted. If a crash occurs during close, the stable client order id supports reconciliation. If a crash occurs after the replacement enters its submission window and the result cannot be found in lifecycle records, the coordinator fails closed and requires operator reconciliation instead of blindly submitting a duplicate.
- Production opportunities are explicitly refused by the automatic switch coordinator. This feature is for controlled paper/test environments only.

The evidence callback must attach the fresh quote's `as_candidate_costs()` mapping to `Opportunity.agent_consensus['execution_costs']`; the final `capital_runtime` guard now forwards those fee/slippage/funding values into `CapitalGateway.authorize_opportunity()` rather than reverting to generic defaults. The evidence callback and venue adapters are deliberately injected rather than replaced with guessed values. A missing callback means no switch decisions; a missing venue-flat confirmation means no close confirmation.

## Venue execution-cost intelligence

`aitos.execution.cost_intelligence.VenueExecutionCostRegistry` requires a registered venue adapter and validates venue/symbol/side identity plus quote freshness. Quotes include taker fees, expected slippage/market impact, signed funding cost over the requested holding horizon, and liquidation distance for the actual leverage/margin mode. Funding credits are represented as negative costs; missing/stale quotes must veto a switch. No universal fee or liquidation-distance estimate is substituted.

## Rollout and validation

- Unit-test the pure decision policy and all vetoes.
- Keep coordinator attachment opt-in; it is not automatically installed during application startup. Do not configure it for production.
- Extend tests for close/replace sequencing, partial fills, timeouts, restart recovery, duplicate events, fee/funding attribution and reservation release. The current tests cover the core state machine and fail-closed unconfirmed-close path; venue-specific integration tests remain required.
- Run historical replay with realistic fees, spread, slippage, funding, market impact and walk-forward partitions; compare against the unchanged hold-only baseline.
- Run paper trading and restart recovery before any production enablement.
- Never relax existing drawdown, liquidation, position-size, portfolio-risk or circuit-breaker controls to increase trade frequency.

This document and pure evaluator do not claim profitability improvement. The coordinator, SQLite journal contract, reduce-only request contract, scan-cycle batch hook and venue quote registry are implemented. Runtime wiring of real venue adapters, the live PositionManager evidence callback, persistent journal path, exchange position-flat query, full failure-injection matrix, historical replay and paper-runtime verification remain outstanding. No profitability claim is made.
