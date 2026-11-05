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

## Execution protocol — required runtime integration

This evaluator is intentionally not an order-execution path. The orchestration layer must:

1. Collect the batch of fresh candidates and rank/allocate them through the existing CapitalGateway.
2. Evaluate only candidates that already have an eligible, reserved allocation.
3. Re-check the incumbent's current thesis, completed-bar stagnation, fees/funding, and live portfolio risk immediately before acting.
4. Ensure the incumbent close is reduce-only and idempotent; wait for confirmed fill/position reconciliation.
5. Release the incumbent's reservation only after confirmed close, then reserve/authorize the replacement against refreshed portfolio state.
6. Submit the replacement only after its capital authorization succeeds. On close failure, partial fill, stale candidate, reservation failure, or risk breach, do not open the replacement.
7. Persist a switch intent/idempotency key and decision evidence so restart recovery cannot duplicate closes or entries.

No exchange order should be sent directly by `DynamicRebalancer`.

## Rollout and validation

- Unit-test the pure decision policy and all vetoes.
- Integrate behind a disabled-by-default runtime feature flag.
- Test close/replace sequencing, partial fills, timeouts, restart recovery, duplicate events, fee/funding attribution and reservation release.
- Run historical replay with realistic fees, spread, slippage, funding, market impact and walk-forward partitions; compare against the unchanged hold-only baseline.
- Run paper trading and restart recovery before any production enablement.
- Never relax existing drawdown, liquidation, position-size, portfolio-risk or circuit-breaker controls to increase trade frequency.

This document and pure evaluator do not claim profitability improvement. Runtime orchestration and exchange-specific cost/liquidation adapters remain separate integration gates.
