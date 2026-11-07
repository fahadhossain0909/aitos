# AITOS Quantitative Intelligence Engine

## Operating contract

Statistical models produce estimates, uncertainty and diagnostics. They do not
place orders, bypass the Risk Engine, or promote themselves. Every estimate
must be tagged to a symbol, horizon, observation window and data timestamp
before a live consumer may use it. A missing, stale or invalid estimate must
degrade to the existing conservative policy, never to a permissive default.

## Phase map and current implementation status

| Phase | Scope | Current state |
|---|---|---|
| 0 — Contracts and data quality | Finite chronological observations, explicit horizons, bounded windows, leakage controls and serialisable outputs | Partial: input validation and serialisable contracts exist; a common timestamped feature-store contract and live freshness enforcement remain |
| 1 — Statistical foundations | Distribution, volatility, Bayesian, HMM, GARCH, EVT and hedge-beta estimates | Partial: existing HMM/GARCH/EVT/Bayes stack plus distribution, Kalman and fitted HAR-RV/GJR-GARCH estimators |
| 2 — Regime and event dynamics | Regime posterior, change-point detection, duration-aware regimes, order-flow event intensity | Partial: Gaussian HMM, CUSUM, BOCPD and a discrete Hawkes-inspired proxy; HSMM and calibrated event-time Hawkes estimation remain |
| 3 — Tail, dependence and portfolio risk | EVT severity, asymmetric volatility, cross-asset dependence, risk sizing and drawdown controls | Partial: POT-GPD, GARCH/GJR-GARCH and a conservative overlay contract; DCC-GARCH/copulas, portfolio ES aggregation and validated live sizing integration remain |
| 4 — Research validation | Purged walk-forward, CPCV, PBO, calibration, regime splits and execution-cost sensitivity | Partial: walk-forward, CPCV, Brier, log-loss, QLIKE and PBO diagnostics; full experiment ledger, bootstrap confidence intervals and end-to-end fee/funding/slippage replay remain |
| 5 — Strategy and risk integration | Strategy-family suitability, model disagreement, risk gate integration and decision attribution | Partial: A-Stat ranking is diagnostic only; strategy execution remains in the existing StrategyEngine/Risk Engine; live statistical risk overlay is not wired |
| 6 — Shadow deployment and governance | Shadow inference, drift monitoring, model registry, champion/challenger, rollback and controlled promotion | Not complete: no production shadow rollout, durable model registry, automated drift gates or governed promotion workflow is claimed |

This status table is intentionally conservative. A module existing in the
repository is not evidence that it is connected to the live scanner, position
sizer, portfolio risk gate or paper-trading runtime.

## Implemented estimators

- Return distribution: downside semivolatility, skew, excess kurtosis,
  quantiles and signal-to-noise.
- Equal-weight multi-horizon realized-volatility baseline and fitted
  one-step HAR-RV regression over squared-return proxies.
- Gaussian HMM regime posterior, GARCH(1,1), asymmetric GJR-GARCH(1,1),
  peaks-over-threshold GPD tail estimate and hierarchical Bayesian shrinkage.
- Two-sided CUSUM and Student-t predictive Bayesian online change-point
  detection (BOCPD) with a bounded run-length posterior.
- Normalized permutation entropy, coarse Hurst screening, scalar Kalman beta,
  and a discrete exponential buy/sell event-excitation proxy.
- Brier score, log loss, QLIKE, purged expanding walk-forward folds,
  combinatorial purged folds and a CSCV-style probability-of-backtest-
  overfitting diagnostic.
- Conservative statistical risk multiplier contract that can only reduce a
  separately calculated base size and cannot override hard limits.

## Estimator limitations

The fitted HAR-RV target is next-observation squared return, not high-frequency
realized variance. GJR-GARCH uses a deterministic constrained grid search,
not a full maximum-likelihood optimizer. BOCPD uses a constant hazard and a
truncated run-length posterior; it is not an HSMM. The HMM is a lightweight
Gaussian implementation and is not a production-calibrated duration model.
The EVT estimator is a method-of-moments POT baseline and requires threshold
stability diagnostics. The event-excitation estimator is not continuous-time
Hawkes maximum likelihood. The Hurst estimate is a coarse screening statistic.
CPCV/PBO are research diagnostics, not substitutes for chronological
walk-forward and a realistic deployment simulation.

## Production acceptance gates

1. **Data contract:** immutable source timestamps, symbol/horizon identity,
   deduplication, event-time ordering, stale-data rejection and feature
   lineage from source to decision.
2. **Estimator validation:** expanding walk-forward and CPCV/PBO where
   appropriate; threshold/model stability; calibration curves; regime-wise
   and symbol-wise metrics; bootstrap confidence intervals.
3. **Execution realism:** maker/taker fees, spread, market impact, funding,
   borrow where relevant, partial fills, latency and rejected/cancelled orders.
4. **Risk integration:** apply any validated statistical multiplier only after
   base sizing and before projected-portfolio hard-limit checks. Preserve
   capital budgets, leverage caps, concentration limits, circuit breakers and
   exchange protection as authoritative.
5. **Shadow operation:** compare forecasts with realised outcomes without
   changing orders; measure Brier/log-loss/QLIKE, calibration drift, model
   disagreement, feature freshness, latency and failure rates.
6. **Governed promotion:** persist model/version/config/data lineage, require
   minimum effective sample size and confidence intervals, use champion/
   challenger comparison, support immediate rollback, and require explicit
   approval before changing production policy.

## Safety

No estimator in this package is evidence of profitability. No automatic
strategy promotion or live sizing change is enabled by this PR. The risk
overlay is a contract and must be calibrated for the same symbol and horizon.
Hard limits, circuit breakers, liquidity checks and execution controls remain
authoritative. Paper-runtime and historical replay evidence are required
before any production wiring is enabled.
