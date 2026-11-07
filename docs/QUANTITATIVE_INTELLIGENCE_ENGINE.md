# AITOS Quantitative Intelligence Engine

## Scope

This package adds dependency-light statistical diagnostics and validation
primitives. It does not claim that a diagnostic is a profitable signal, and
it does not execute orders.

## Implemented primitives

- Return distribution: downside semivolatility, skew, excess kurtosis,
  quantiles and signal-to-noise.
- Multi-horizon realized-volatility baseline (HAR-style), downside volatility
  and a descriptive jump proxy.
- Two-sided standardized CUSUM change-point diagnostic.
- Normalized permutation entropy and a coarse Hurst screening estimate.
- Discrete exponential buy/sell event-excitation proxy inspired by Hawkes
  processes.
- Scalar random-walk Kalman hedge-beta estimator.
- Brier score, log loss and QLIKE forecast losses.
- Expanding walk-forward folds with forward-label purge.
- A conservative statistical risk multiplier that can only reduce a base
  position size and never overrides hard risk limits.

## Explicit limitations

The HAR-style estimator is an equal-weight baseline, not a fitted HAR-RV
regression. The event-excitation estimator is not continuous-time Hawkes
maximum likelihood. CUSUM is a diagnostic, not Bayesian online change-point
detection. The Hurst estimate is a coarse screening statistic. The fold
generator is purged walk-forward, not CPCV. No model is promoted automatically.

Production integration requires symbol/horizon-specific calibration, immutable
feature timestamps, deduplicated event histories, realistic fees/funding/
slippage, out-of-sample evaluation, regime stratification and paper-runtime
verification. The overlay must be applied only after base sizing and before
the existing hard-limit checks; hard limits and circuit breakers remain
authoritative.
