# AITOS Universal Strategy Layer v1

PR #116 keeps the existing directional AITOS stack intact and adds a strategy operating layer above the shared risk, portfolio and execution boundaries.

## Invariants

1. Strategies never call an exchange or broker directly.
2. Every order proposal is an `ExecutionIntent` and must pass the existing pre-trade risk gate before an adapter can execute it.
3. Capital is allocated centrally; strategies cannot reserve unlimited capital.
4. Regime selection changes which strategy families are eligible, not the safety boundaries.
5. Venue, asset class and ticker syntax remain outside strategy logic.
6. New strategies are plugins registered through `StrategyRegistry`.
7. Paper/shadow execution is the default integration path for these new strategies.

## Strategy families

- `directional`: existing AITOS trend/AMT/order-flow/lead-lag strategies.
- `arbitrage`: cross-market and statistical relative-value strategies.
- `funding_basis`: funding-rate and spot/perpetual basis opportunities.
- `market_making`: liquidity/spread capture with inventory constraints.
- `hedging`: portfolio delta and cross-instrument risk reduction.
- `options`: volatility/option-specific opportunities.
- `regime`: routing based on bull, bear, sideways, high-volatility and low-volatility states.
- `special`: future event/structure-specific strategies.

## Lifecycle

```text
MarketData + CrossMarket Intelligence + Global Regime
                         |
                         v
                 StrategyRegistry
                         |
                         v
                  StrategyEngine
                         |
                  CapitalAllocator
                         |
                         v
                 Pre-trade Risk Gate
                         |
                         v
                Portfolio/Position Mgmt
                         |
                         v
                   ExecutionIntent
                         |
                         v
                 ExecutionAdapter
```

The current built-ins are conservative intent generators. They are deliberately not wired to live execution. A production rollout should first run them in paper/shadow mode, measure net edge after fees, slippage, basis, funding and market impact, then enable a family through configuration.

## Funding-farming protection

A funding opportunity is evaluated as net edge rather than headline funding:

`net edge = funding - basis cost - fees - expected slippage - market impact - hedge/exit cost`

This prevents a positive funding rate from being treated as guaranteed profit.

## Market-making protection

The market-making plugin requires a minimum spread and positive liquidity score, limits inventory notional, and emits bid/ask intents around the observed mid. Venue-specific quoting, tick size, post-only flags and order lifecycle remain execution-adapter responsibilities.
