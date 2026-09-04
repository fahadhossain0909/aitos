# AITOS Capital Growth & Protection Objective

## Policy

AITOS optimizes for **maximum sustainable capital growth subject to strict capital-protection constraints**.

The system must not select an asset merely because its directional return estimate is high. Asset selection is a capital-allocation decision: every opportunity is evaluated for expected net edge, loss probability, loss severity, execution cost, liquidity and confidence.

## Decision hierarchy

1. **Capital survival/protection is a hard constraint.** A high-return estimate cannot override a protection violation.
2. **Trading must have positive net edge.** Fees, slippage and funding are deducted before ranking.
3. **No-trade is a valid decision.** If no opportunity clears the objective, capital remains undeployed.
4. **Among eligible opportunities, maximize sustainable growth.** Growth is weighted 60% and protection 40% by default.
5. **Capital allocation is risk-budget based.** Position notional is derived from an approved risk budget, not from leverage appetite.

## Economic model

For an opportunity estimate:

`expected_net_edge = expected_gross_return - total_cost - (loss_probability × expected_loss)`

where `total_cost` includes trading fee, expected slippage and funding/financing cost.

The default hard gates are:

- minimum net edge: 0.05%
- minimum expected return after costs: 0.10%
- maximum loss probability: 35%
- maximum expected loss severity: 1.50%
- maximum total cost: 0.50%
- minimum liquidity score: 4/10

These are configuration defaults, not claims about future market performance. They should be calibrated from AITOS backtests and paper-trading telemetry before any production policy change.

## Architecture placement

`aitos/intelligence/capital_objective.py` is venue-neutral and asset-class-neutral. Strategy/market intelligence supplies an `OpportunityEstimate`; the objective evaluates it; the allocator converts eligible decisions into bounded risk budgets.

This keeps indicators, order flow, auction-market analysis, lead/lag, AI/RL and market-regime models as evidence sources rather than allowing any single feature to define the portfolio objective.

The same contract can therefore rank crypto, equities, FX, futures, commodities, rates, bonds, options or indices once the corresponding market/execution adapters provide the required estimates.
