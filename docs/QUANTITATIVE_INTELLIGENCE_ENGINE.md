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
| 3 — Tail, dependence and portfolio risk | EVT severity, asymmetric volatility, cross-asset dependence, risk sizing and drawdown controls | Partial: downside and upside POT-GPD tails, GARCH/GJR-GARCH and a side-matched freshness-gated reduce-only scanner-to-lifecycle overlay; DCC-GARCH/copulas, portfolio ES aggregation and replay calibration remain |
| 4 — Research validation | Purged walk-forward, CPCV, PBO, calibration, regime splits and execution-cost sensitivity | Partial: walk-forward, CPCV, Brier, log-loss, QLIKE and PBO diagnostics; full experiment ledger, bootstrap confidence intervals and end-to-end fee/funding/slippage replay remain |
| 5 — Strategy and risk integration | Strategy-family suitability, model disagreement, risk gate integration and decision attribution | Partial: A-Stat ranking is diagnostic only; fresh matched statistical forecasts reduce base size before final hard-limit checks; strategy execution remains in the existing StrategyEngine/Risk Engine |
| 6 — Shadow deployment and governance | Shadow inference, drift monitoring, model registry, champion/challenger, rollback and controlled promotion | Partial: SQLite shadow forecasts and FeatureSnapshots are persisted at trade entry and resolved against net trade outcomes at close; durable artifact registry and explicit champion/rollback APIs are implemented, but live drift orchestration and automatic promotion are not enabled |

This status table is intentionally conservative. A module existing in the
repository is not evidence that it is connected to the live scanner, position
sizer, portfolio risk gate or paper-trading runtime.

## Implemented estimators

- Return distribution: downside semivolatility, skew, excess kurtosis,
  quantiles and signal-to-noise.
- Uncertainty-aware forecast ensemble using the law of total variance to retain
  between-model forecast disagreement.
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
- Scanner-to-lifecycle forecast handoff with explicit symbol/side/timeframe/model
  identity, sample-size and 30-second freshness checks; long and short forecasts
  use their respective downside/upside EVT tails. The reduce-only assessment is
  retained in the opened trade's decision metadata.
- Trade-entry/close shadow ledger wiring with idempotent forecast IDs, persisted
  FeatureSnapshot payloads, resolved-only calibration metrics and SQLite WAL.
- Durable model-artifact registry with immutable SHA-256/config-hash registration,
  one-champion-per-model enforcement, evidence-gated promotion and explicit
  operator-approved rollback; no artifact is loaded or promoted automatically.
- Paired block-bootstrap OOS confidence intervals and PSI feature-drift reports.

## Estimator limitations

The fitted HAR-RV target is next-observation squared return, not high-frequency
realized variance. GJR-GARCH uses a deterministic constrained grid search,
not a full maximum-likelihood optimizer. BOCPD uses a constant hazard and a
truncated run-length posterior; it is not an HSMM. The HMM is a lightweight
Gaussian implementation and is not a production-calibrated duration model.
The downside/upside EVT estimator is a method-of-moments POT baseline and
requires threshold stability diagnostics. Shadow-ledger realised variance is
currently a trade-level squared net-return proxy, not high-frequency realised
variance; QLIKE results must be interpreted accordingly. The event-excitation estimator is not continuous-time
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
   changing orders; the current trade-level squared-return variance proxy is
   explicitly not a high-frequency realised-variance series. Measure
   Brier/log-loss/QLIKE, calibration drift, model disagreement, feature
   freshness, latency and failure rates.
6. **Governed promotion:** persist model/version/config/data lineage, require
   minimum effective sample size and confidence intervals, use champion/
   challenger comparison, support immediate rollback, and require explicit
   approval before changing production policy.

## Safety

No estimator in this package is evidence of profitability. No automatic
strategy promotion is enabled. On this branch, a fresh, matched statistical
forecast can only reduce the already calculated base position size; malformed,
stale or mismatched supplied forecasts fail closed. This integration still
requires calibration and paper-runtime/historical-replay evidence before
production use. Hard limits, circuit breakers, liquidity checks and execution
controls remain authoritative.
