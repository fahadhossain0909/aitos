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
| 2 — Regime and event dynamics | Regime posterior, change-point detection, duration-aware regimes, order-flow event intensity | Partial: Gaussian HMM, CUSUM, BOCPD, explicit-duration Gaussian HSMM and marked continuous-time exponential Hawkes profile-MLE; out-of-sample duration/intensity calibration and production feed wiring remain |
| 3 — Tail, dependence and portfolio risk | EVT severity, asymmetric volatility, cross-asset dependence, risk sizing and drawdown controls | Partial: downside/upside POT-GPD, GARCH/GJR-GARCH, DCC(1,1)-GARCH, empirical Gaussian-copula dependence, historical portfolio VaR/ES and reduce-only risk overlay; market-regime calibration and full portfolio replay remain |
| 4 — Research validation | Purged walk-forward, CPCV, PBO, calibration, regime splits and execution-cost sensitivity | Partial: walk-forward, CPCV, PBO, Brier/log-loss/QLIKE, symbol/regime cohort reports and event-time L1 execution replay with fees, spread, slippage, latency, partial fills, funding and borrow; full-depth impact, queue position, venue-specific fee/funding schedules and market-data-backed replay remain |
| 5 — Strategy and risk integration | Strategy-family suitability, model disagreement, risk gate integration and decision attribution | Partial: fresh matched forecasts reduce base size before hard limits; scanner gate decisions, entries, rejections, HOLD/MANAGE/EXIT and execution failures persist under trace identity and reconcile to net close outcomes; end-to-end completeness across every execution adapter and non-trade candidate lifecycle still requires runtime audit |
| 6 — Shadow deployment and governance | Shadow inference, drift monitoring, model registry, champion/challenger, rollback and controlled promotion | Partial: SQLite shadow forecasts and decision ledger reconcile to outcomes; durable registry and audit events exist; opt-in runtime drift controller blocks promotion on drift and requires explicit operator approval. Continuous live feature-batch scheduling, alert routing and operator-facing approval workflow remain |

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
- Gaussian HMM and explicit-duration Gaussian HSMM regime posteriors,
  marked continuous-time exponential Hawkes profile-MLE, GARCH(1,1),
  asymmetric GJR-GARCH(1,1), peaks-over-threshold GPD tail estimate and
  hierarchical Bayesian shrinkage.
- DCC(1,1)-GARCH conditional correlation, empirical Gaussian-copula
  rank-dependence, and aligned historical-simulation portfolio VaR/expected
  shortfall aggregation.
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
  FeatureSnapshot payloads, resolved-only calibration metrics and SQLite WAL;
  multiprocess write contention, schema migration and unresolved-record queries
  have regression coverage. TradeStatePersistence.restore replays durable open
  and closed trades into the shadow ledger idempotently after restart.
- Durable model-artifact registry with immutable SHA-256/config-hash registration,
  one-champion-per-model enforcement, persisted decision/evidence audit events,
  evidence-gated promotion and explicit operator-approved rollback; no artifact
  is loaded or promoted automatically.
- Paired block-bootstrap OOS confidence intervals, PSI feature-drift reports,
  gross-to-net execution-cost sensitivity, and symbol/regime cohort metrics.
- Trace-linked decision ledger for scanner GATEs, accepted ENTRYs, REJECTIONs,
  HOLD/MANAGE/EXIT actions and execution failures, reconciled against net trade
  outcomes.
- Event-time L1 execution replay with submission latency, displayed-size-limited
  partial fills, bid/ask spread, adverse slippage, taker fees, prorated funding
  and borrow costs. It deliberately does not model full-depth market impact or
  queue priority.
- Opt-in governance runtime that consumes reference/current feature batches,
  marks NORMAL/WARNING/CRITICAL drift, blocks challenger promotion on drift,
  and requires explicit approval at the promotion call. It does not autonomously
  switch production models or disable trading.

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
Hawkes maximum likelihood. The HSMM initializes Gaussian emissions by one-dimensional clustering and
estimates explicit duration distributions from observed state runs; duration
stability and state-count selection remain out-of-sample research tasks. DCC
uses a constrained parameter grid and Gaussian quasi-likelihood; the copula is
an empirical Gaussian rank transform, not a tail-dependent copula family.
Portfolio VaR/ES is historical simulation and requires representative joint
market scenarios. The L1 replay consumes supplied top-of-book snapshots and
cannot infer hidden liquidity, queue position, or market impact beyond
displayed size. The Hurst estimate is a coarse screening statistic.
CPCV/PBO are research diagnostics, not substitutes for chronological
walk-forward and a realistic deployment simulation.

## Production acceptance gates

1. **Data contract:** immutable source timestamps, symbol/horizon identity,
   deduplication, event-time ordering, stale-data rejection and feature
   lineage from source to decision.
2. **Estimator validation:** expanding walk-forward and CPCV/PBO where
   appropriate; threshold/model stability; calibration curves; regime-wise
   and symbol-wise metrics; bootstrap confidence intervals.
3. **Execution realism:** gross-to-net cost sensitivity is implemented, but it
   is not yet wired to historical fill/market-impact replay. Maker/taker fee
   schedules, spread, slippage, funding, borrow, partial fills, latency and
   rejected/cancelled orders still require data-backed end-to-end replay.
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
   approval before changing production policy. The opt-in governance controller
   currently accepts reference/current feature batches; it still needs a
   production scheduler, alert sink, and authenticated operator approval path.

## Safety

No estimator in this package is evidence of profitability. No automatic
strategy promotion is enabled. On this branch, a fresh, matched statistical
forecast can only reduce the already calculated base position size; malformed,
stale or mismatched supplied forecasts fail closed. This integration still
requires calibration and paper-runtime/historical-replay evidence before
production use. Hard limits, circuit breakers, liquidity checks and execution
controls remain authoritative.


## Synthetic validation suite

A deterministic, seeded pytest suite is maintained at
`tests/statistics/test_synthetic_market_validation.py`. It exercises the
HSMM posterior, marked Hawkes stationarity, correlated multi-asset DCC and
portfolio tail aggregation, L1 execution-cost accounting, chronological
purged/CPCV splits, calibration metrics, paired block bootstrap and PBO
diagnostics. It uses synthetic observations only and is not evidence of
profitability or a substitute for exchange-data replay.

The suite must pass in the repository CI matrix before this branch can be
considered code-validated. A successful unit-test run still does not establish
paper-runtime integration, production calibration, full-depth market impact,
or real-world execution quality.
