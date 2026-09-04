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
6. **Portfolio protection is applied after opportunity ranking.** Correlation/concentration, regime/volatility and drawdown can reduce or veto an otherwise eligible allocation.
7. **Capital is reserved before execution.** The reservation ledger prevents simultaneous opportunities from oversubscribing capital or the portfolio risk budget.
8. **TradeLifecycle is the final capital boundary.** An opportunity that is not capital-authorized is returned as `REJECTED` and never reaches order submission.

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

## Portfolio protection

`aitos/intelligence/capital_protection.py` adds four P0 controls:

- **Correlation/concentration gate:** existing position risk is multiplied by pairwise correlation. Missing correlation is conservatively treated as 0.75 rather than zero, preventing a whole-market crypto portfolio from falsely appearing diversified.
- **Dynamic risk budget:** high-volatility conditions reduce the requested risk to 50%; extreme volatility reduces it to 25%. `risk_off` and `high_volatility` regimes also default to 50%, while `transition` defaults to 75%.
- **Drawdown-aware sizing:** risk is reduced at 3%, 5% and 8% drawdown levels (75%, 50%, 25% multipliers respectively), and new risk is stopped at 10% drawdown.
- **Capital reservation:** a thread-safe, idempotent reservation ledger blocks simultaneous allocations that exceed available capital or the portfolio risk budget. Reservations are released on cancellation/close through `CapitalGateway.release()`.

These are conservative policy defaults and must be calibrated against AITOS paper-trading/backtest telemetry. They are not performance guarantees.

## Runtime enforcement

`aitos/intelligence/capital_gateway.py` converts an executable `Opportunity` into the venue-neutral economic estimate. The nearest take-profit is used as the conservative gross-return target. The stop distance supplies loss severity. A calibrated `loss_probability` supplied in `agent_consensus` takes precedence; otherwise the kernel/scanner confidence is converted to a conservative probability-like signal.

The gateway then applies portfolio protection and reserves the approved capital/risk budget. `aitos/intelligence/capital_runtime.py` installs the guard on `TradeLifecycle.submit_opportunity`. It is fail-closed: invalid/unavailable equity, malformed economic inputs, failed objective gates, protection failures, reservation failures and missing allocations cannot reach the original lifecycle submission path.

The guard also records the approved risk budget in `agent_consensus["capital_objective"]` so the authorization is auditable by the journal/telemetry layers.

## Execution-cost defaults

Until venue-specific fee/slippage/funding models are supplied, the runtime boundary uses conservative defaults of **10 bps fee + 5 bps slippage + 0 bps funding**. These are policy assumptions, not exchange guarantees. They should be replaced with live venue/account-specific estimates before production deployment.

## Architecture placement

`aitos/intelligence/capital_objective.py` is venue-neutral and asset-class-neutral. Strategy/market intelligence supplies an `OpportunityEstimate`; the objective evaluates it; the allocator converts eligible decisions into bounded risk budgets; portfolio protection then applies portfolio-aware constraints before reservation and execution.

This keeps indicators, order flow, auction-market analysis, lead/lag, AI/RL and market-regime models as evidence sources rather than allowing any single feature to define the portfolio objective.

The same contract can therefore rank crypto, equities, FX, futures, commodities, rates, bonds, options or indices once the corresponding market/execution adapters provide the required estimates.
