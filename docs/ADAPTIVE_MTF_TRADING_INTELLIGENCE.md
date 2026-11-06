# Adaptive Multi-Timeframe Trading Intelligence — Implementation Notes

## Scope and safety

This branch extends AITOS decision intelligence only. It does not place orders, connect to a live exchange account, or enable live trading. Paper-trading/runtime market verification is intentionally deferred to the user's later consolidated session.

## P0 — Directional diagnostics

- Scanner emits a symmetric LONG/SHORT directional funnel record.
- Both sides expose structure pass, CVD pass, bias conflict, diagnostic acceptance, and explicit rejection reason codes.
- CHoCH/CVD thresholds remain symmetric: LONG >= 6.5 and SHORT <= 3.5. Threshold relaxation is not inferred from diagnostics.

## P1/P2 — Adaptive six-timeframe analysis

- Added a candle-close-aware multi-timeframe orchestration layer for **1D, 4H, 1H, 15M, 5M and 1M**.
- Every timeframe uses the shared `HigherTimeframeCandleCache`; cache refresh is keyed by symbol/timeframe/lookback and single-flight protected.
- Each frame reports latest closed candle timestamp, age, permitted freshness age, ADX, DI gap, structure bias, regime and cache state.
- Forming candles are excluded by the cache. A stale fallback, insufficient history or missing timeframe marks the stack incomplete.
- The scanner requires all six frames to be fresh and complete before accepting an MTF decision. It does not convert unavailable data into neutral evidence.
- All six timeframe contexts are evaluated together using a configurable strength-weighted consensus threshold. A single lower-timeframe counter-move does not automatically veto a higher-timeframe majority; balanced stacks require the LTF quality floor, and dominant countertrend stacks require strong structure + order-flow reversal evidence.
- Cache request budget, fetch concurrency and freshness grace are centrally configurable with environment overrides.
- The primary timeframe map and legacy single-HTF helper remain for compatibility; the scanner decision path now uses the six-frame analyzer.

## P3 — Expected-value entry and exit integration

- Entry authorization is enforced at the `TradeLifecycle.submit_opportunity` boundary by `CapitalGateway`, not only by scanner ranking.
- Entry EV includes calibrated loss probability, nearest TP gross return, stop loss, fee, slippage, funding, liquidity, confidence, opportunity age decay and minimum net-edge/expected-return thresholds.
- Capital authorization fails closed when EV, cost, loss probability, liquidity, age, circuit-breaker or portfolio protection gates fail.
- Exit Intelligence Engine already calculates probability-weighted path continuation value. This branch exposes its expected gain, expected loss, upside/downside probability mass and transaction-cost components in the decision feature record.
- Exit decisions continue to require structural/thesis invalidation or confirmed exit pressure; a static TP touch is not treated as a reason to abandon a still-valid runner thesis.
- The path-based exit expectation is a heuristic probability-weighted estimate, not a calibrated statistical forecast. Calibration against out-of-sample realized path outcomes remains a research requirement.

## P4 — Capital allocation and correlation-aware portfolio risk

- The lifecycle capital guard calls the shared CapitalGateway before sizing/execution.
- The gateway applies expected-edge ranking, per-trade/portfolio risk budgets, drawdown and volatility multipliers, circuit breakers, capital reservations and portfolio protection.
- Existing portfolio correlation data is consumed by PortfolioProtection. Unknown correlations use a conservative configured fallback; known correlations increase correlated-risk usage and constrain incremental risk.
- The gateway's approved risk budget is passed into the trade sizing path; the capital gate cannot be bypassed by calling lifecycle submission directly.
- Correlation estimates must be point-in-time, symmetric and refreshed from measured returns. A single portfolio-wide max-pairwise fallback is conservative but not a substitute for a complete covariance matrix.

## P5 — Historical replay, walk-forward and ablation evaluation

- Added leakage-aware chronological split generation with a configurable embargo.
- Evaluation rows require timezone-aware decision/outcome timestamps, outcome strictly after decision, finite net PnL and explicit LONG/SHORT side.
- Held-out fold summaries report trade count, expectancy, win rate, profit factor, drawdown and LONG/SHORT-separated metrics.
- Comparative reports support baseline versus ablation variants (for example removing order-flow, MTF, liquidity or footprint evidence) and calculate deltas where metrics are defined.
- Added `HistoricalAblationRunner`, which executes baseline and component-disabled strategy callables through the canonical `BacktestEngine`, then runs each variant through the existing `WalkForwardValidator` with a locked holdout.
- The ablation report includes full replay metrics, walk-forward/holdout outcomes, LONG/SHORT-separated realized PnL metrics, unclassified-trade counts and deltas versus baseline. Strategy variants are supplied as named callables so OFI/order-flow/MTF/liquidity/footprint components can be switched off without silently changing the baseline.
- The leakage-aware observation helpers remain available for externally recorded journals; they do not synthesize historical data or claim a backtest result without the canonical replay.
- No model/strategy is promoted automatically. Use a locked holdout after walk-forward and keep paper/shadow validation as a separate approval gate.

## Tests authored

- Six-frame fetch coverage and stale-stack fail-closed behavior.
- LONG/SHORT directional diagnostics and adaptive HTF behavior.
- EV entry/cost checks already present in capital runtime tests.
- Probability-weighted exit expectancy diagnostics and mirrored SHORT path handling.
- Correlation-aware incremental portfolio risk.
- Chronological walk-forward embargo, timestamp leakage rejection, ablation reporting and LONG/SHORT comparative metrics.

## Verification status

- New and existing tests are authored for CI/regression verification.
- This branch's test suite and GitHub Actions must be run and reviewed before the work can be called regression-verified.
- Exchange-connected runtime, paper trading, restart recovery against a live market stream, and execution-quality verification are deliberately not part of this phase.
- No profitability or performance improvement is claimed without historical replay on point-in-time data and a locked out-of-sample holdout.


## CI follow-up

Formatting changes were applied after the initial CI report. Re-run CI on the latest branch head before treating regression verification as complete.
