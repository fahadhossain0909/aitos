# A-Stat: predictive statistical intelligence

A-Stat is the market-agnostic statistical layer introduced on top of architecture v1 (PR #116). It is an evidence and probability engine, not an execution engine.

## Contract

```text
MarketEvent / calculated features / cross-market intelligence
                         |
                         v
                     A-Stat
                         |
        +----------------+----------------+
        |                |                |
   Direction          Regime         Volatility
 probability        probability       estimate
        |                |                |
        +----------------+----------------+
                         |
                expected return / EV
                         |
                  downside / tail risk
                         |
                 calibrated confidence
                         |
                  StrategyStatContext
                         |
       +-----------------+------------------+
       |                 |                  |
  directional         hedging            options
```

The `StrategyStatContext` boundary means strategy implementations consume statistical evidence without depending on exchange/ticker conventions. PR #116 already defines universal instruments, market events, cross-market intelligence, global regime state, portfolio/risk boundaries and execution intents; A-Stat therefore lives beside intelligence and feeds strategies rather than owning execution.

## Current models

- Logistic-style feature score for directional probability.
- Bayesian odds update using explicit likelihood-ratio evidence.
- Regime probability state for trend-up, trend-down, range, high-volatility and low-volatility conditions.
- Online realised-volatility estimate with a deterministic fallback.
- Expected-value estimate from directional probabilities and win/loss magnitudes.
- Downside/tail probability estimate.
- Online Brier-based calibration quality and sample-size confidence.

The implementation uses Python's standard library only. This keeps the production image lightweight while leaving a stable contract for later HMM/Markov-switching, GARCH, EVT and richer Bayesian implementations.

## Strategy use

A strategy should request/evaluate a statistical observation and then consume:

```python
context = result.for_strategy("directional")
context.direction.up
context.expected_value
context.expected_volatility
context.downside_probability
context.regime.trend_up
context.suitability
```

Hedging and options strategies use the same context contract but interpret different fields: hedging emphasizes downside/tail risk and regime/correlation context; options emphasizes volatility and expected-value relationships. No strategy is automatically enabled or executed by A-Stat.

## Safety invariants

1. Probabilities are normalised and bounded.
2. Evidence must have positive likelihood ratios.
3. Unknown feature names are ignored.
4. A-Stat has no exchange API or order-placement dependency.
5. Statistical output is advisory until the existing contextual decision and risk gates accept it.
6. Historical/live model calibration must be monitored before promoting richer models to production.
