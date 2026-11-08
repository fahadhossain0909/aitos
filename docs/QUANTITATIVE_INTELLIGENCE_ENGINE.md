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
  and borrow costs. The separate L2 replay walks every supplied price level,
  applies a configurable square-root participation impact curve, and supports
  passive queue-ahead replay from side-classified trades/cancellations/additions.
- Cross-adapter attribution decorator for the shared OrderExecutor contract.
  It records ENTRY/EXIT/MANAGE and EXECUTION_FAILURE outcomes for simulated,
  Binance, cTrader and MT5 adapters. TradeLifecycle rejects zero-fill positions
  and reconciles actual partial-fill quantity/notional/risk before opening a
  position. Binance cancels unfilled market-order remainders; cTrader and MT5
  resting LIMIT/STOP requests fail closed until status/cancel lifecycle support
  is available.
- Opt-in governance runtime with a managed A-Stat async drift worker. It consumes
  reference/current feature batches, emits NORMAL/WARNING/CRITICAL state,
  deduplicates alert callbacks, blocks challenger promotion on drift, and
  requires explicit operator approval. The worker is started/stopped through
  the A-Stat engine lifecycle; it is not silently started at import time.
- Multiple-testing-aware promotion evidence: DSR probability, MCS inclusion,
  White Reality Check, Diebold-Mariano and adjusted p-values are required by
  the default champion-promotion policy.

## Estimator limitations

The Gaussian HSMM separates training-only `fit()` from causal `filter()` and
returns the full posterior history for multiclass OOS calibration. Its Gaussian
emissions are initialized by one-dimensional clustering and its duration laws
are empirical run-length estimates, not a fully optimized EM posterior; state
count and duration stability still require broad OOS study. The marked
continuous-time exponential Hawkes estimator now uses constrained projected
gradient MLE with a monotone backtracking line search, a profiled/refined decay
grid and a subcritical branching constraint. The online event filter is exposed
through A-Stat, but the application event consumer must register a fitted model
and forward timestamped marked events.

GJR-GARCH uses constrained QML grid initialization plus coordinate refinement;
EGARCH(1,1) uses constrained Gaussian QML refinement. HAR-RV uses non-negative
least squares. Its target remains next-observation squared return, not
high-frequency realized variance. BOCPD uses a constant hazard and truncated
run-length posterior; it is not an HSMM. The downside/upside EVT estimator is
a method-of-moments POT baseline and still requires threshold stability
diagnostics. Shadow-ledger realised variance is a trade-level squared net-return
proxy, not high-frequency realised variance.

DCC-GARCH still uses a constrained parameter grid; the Student-t copula is a
rank pseudo-likelihood fit with a finite degrees-of-freedom profile grid and
pairwise tail-dependence estimates. Portfolio VaR/ES remains historical
simulation, now accompanied by Kupiec coverage, Christoffersen independence
and realised-to-forecast ES severity diagnostics. These tests require
representative joint scenarios and adequate exception counts.

The L2 replay consumes supplied snapshots and cannot infer hidden liquidity.
Its impact coefficient must be calibrated by venue/symbol/volatility from
historical fills. Passive queue replay requires side-specific queue-ahead
cancellation/replenishment attribution; aggregate depth deltas alone are not
enough. The MCS implementation uses a step-down T-max block bootstrap with
synchronized cross-model resampling; DM uses a HAC variance and normal
approximation. PSR/DSR, Reality Check, MCS, DM and Holm/BH are research
diagnostics, not proof of profitability. The OOS orchestrator supplies
purged symbol/regime fold isolation and accepts a model-specific fit/predict
callback; it does not choose or train a strategy on its own.

## Production acceptance gates

1. **Data contract:** immutable source timestamps, symbol/horizon identity,
   deduplication, event-time ordering, stale-data rejection and feature
   lineage from source to decision.
2. **Estimator validation:** run the new seeded synthetic tests, then expanding
   walk-forward and CPCV/PBO where appropriate; validate threshold/model
   stability, multiclass and probability calibration, symbol/regime metrics,
   bootstrap confidence intervals and selection diagnostics.
3. **Execution realism:** L1/L2 replay primitives now account for latency,
   partial fills, fees, spread, funding/borrow, participation impact and
   queue-ahead events. Venue-specific impact/queue coefficients and event
   classification still require historical execution data and adapter-level
   replay before paper/live claims.
4. **Risk integration:** apply any validated statistical multiplier only after
   base sizing and before projected-portfolio hard-limit checks. Preserve
   capital budgets, leverage caps, concentration limits, circuit breakers and
   exchange protection as authoritative.
5. **Shadow operation:** compare forecasts with realised outcomes without
   changing orders; trade-level squared-return variance remains a proxy, not
   high-frequency realised variance. Measure Brier/log-loss/QLIKE, multiclass
   calibration, VaR/ES exceptions, drift, freshness, latency and failures.
6. **Governed promotion:** persist model/version/config/data lineage; require
   minimum effective sample size, DSR/MCS/Reality-Check/DM/multiple-testing
   evidence, OOS improvement bounds, stable drift and explicit operator
   approval. The drift worker and alert callback are available but require
   deployment-specific feature-batch providers, alert sinks and authenticated
   operator identity. No automatic production promotion is enabled.

## Safety

No estimator in this package is evidence of profitability. No automatic
strategy promotion is enabled. On this branch, a fresh, matched statistical
forecast can only reduce the already calculated base position size; malformed,
stale or mismatched supplied forecasts fail closed. This integration still
requires calibration and paper-runtime/historical-replay evidence before
production use. Hard limits, circuit breakers, liquidity checks and execution
controls remain authoritative.


## Synthetic validation suite

Deterministic, seeded pytest coverage is distributed across
`tests/statistics/test_synthetic_market_validation.py`,
`test_hsmm_hawkes_dependence_replay.py`, `test_tail_calibration.py`,
`test_selection_diagnostics.py`, `test_symbol_regime_experiments.py`,
`test_depth_replay.py`, `test_engine_drift_lifecycle.py`,
`test_advanced_volatility_estimators.py`, and execution/attribution tests.
It exercises leakage-free HSMM fit/filter and multiclass calibration, marked
Hawkes MLE/online updates, DCC plus Student-t copula and VaR/ES backtests,
GJR/EGARCH/HAR estimation, L1/L2 fills and queue priority, purged
symbol/regime OOS orchestration, PSR/DSR/MCS/DM/Reality Check and
multiple-testing corrections, drift scheduling, decision lifecycle and
cross-adapter attribution. All synthetic suites use generated data only; they
are not evidence of profitability or a substitute for historical exchange
execution replay.

The full repository CI matrix and regression suite must pass on the latest PR
head before code validation can be declared. Even a green CI run does not
establish production calibration, venue-specific market-impact coefficients,
live event-consumer wiring, paper-runtime reconciliation or real-world
execution quality.


## Financial machine-learning layer (new foundations)

The aitos.statistics.financial_ml module adds deterministic, dependency-light
research primitives:

- Volume, dollar and signed-volume imbalance bars. Only threshold-complete
  bars are emitted; the incomplete tail is intentionally retained by the
  caller rather than misrepresented as a closed sample.
- Fixed-width fractional differentiation with causal newest-to-oldest weights
  and explicit warm-up null values. Fit thresholds and feature choices on
  training windows only; do not use a future-fitted differencing order.
- Side-aware triple-barrier labels with volatility-scaled profit/stop barriers,
  vertical maximum holding period, conservative stop-first handling when both
  OHLC barriers are touched in the same bar, and explicit fee/slippage/funding
  costs. Volatility inputs must be known at event time.
- A deterministic logistic MetaLabeler that estimates whether a primary
  strategy proposal achieved a positive net outcome. It is an acceptance
  estimate only: it does not generate direction, submit orders, resize beyond
  existing sizing policy, or bypass hard Risk/HTF/execution gates.
- Sequential bootstrap indices weighted by marginal event uniqueness to
  reduce repeated sampling of overlapping labels.

These are primitives, not a production-ready training pipeline. The next
integration step is to bind labels to immutable candidate/decision IDs and
label_end timestamps, then create training rows only after each label has
resolved. Use expanding purged walk-forward folds and embargo before fitting;
keep calibration data separate from model fitting; report executed outcomes
separately from replay-derived counterfactual labels. Rejected candidates must
never be reported as realised PnL. White Reality Check and multiple-testing
corrections are already available in selection.py and should be applied to
the complete strategy/model search family, not just the winning experiment.

No meta-model output is a standalone permission to trade. Promotion remains
manual and governed; no automatic promotion or live trading is enabled.


## Financial Machine Learning phases A-G — integrated implementation

The Financial ML path is now wired into the live scanner and paper-trading
lifecycle. The integration is additive and remains **shadow-first**.

### Phase A — event-time sampling and fractional differentiation

- Accepted canonical live trades are converted into validated MarketEvent
  records with aggressor-side signed volume.
- Independent Volume, Dollar and Imbalance bar accumulators run incrementally
  in SQLite. Completed bars and unfinished per-symbol/per-bar-type accumulators
  survive process restart; the hot path is O(1) per trade.
- The scanner uses completed volume-bar closes for causal fractional-return
  features. Warm-up observations are omitted rather than backfilled.
- Default research thresholds are 25 base-volume units, 250,000 quote dollars
  and 8 signed-volume units. These are starting defaults, not universal
  instrument calibrations.

### Phase B — Triple Barrier labels and outcome provenance

- Each scanner candidate receives a UUID-derived stable candidate_id, an
  event-time feature snapshot, entry reference, ATR/price volatility estimate,
  upper/lower barriers and a 15-minute vertical expiry.
- Rejected candidates enter the counterfactual observation state. Live trade
  ticks resolve the first observed take/stop touch or vertical expiry, recording
  the exact label_resolution_time, gross/net return, assumed costs and
  COUNTERFACTUAL_LIVE_MARKOUT provenance.
- The offline OHLC Triple Barrier function supports LONG and SHORT, vertical
  expiry, fee/slippage/funding bps, conservative stop-first intrabar ambiguity,
  and opening-gap fills at the observed open.
- A candidate that is actually filled transitions to EXECUTED; counterfactual
  ticks cannot resolve it. On close, the lifecycle supplies realised net return
  including actual recorded fees and funding under
  EXECUTED_TRADE_NET_OUTCOME provenance.
- Counterfactual labels are never represented as realised PnL. The default
  counterfactual round-trip cost assumption is 8 bps and must be calibrated
  against the venue. Actual executed outcomes use the trade ledger's realised
  net result.

### Phase C — Meta-Labeling, calibration and acceptance

- The existing primary strategy still owns direction. The logistic meta-model
  estimates whether that primary proposal is likely to produce a positive
  net outcome; it cannot generate a side or override Risk, MTF/HTF, kernel or
  execution safeguards.
- Standardization is fit on each training fold only. Platt scaling is fit on
  an early chronological out-of-fold segment; calibration and acceptance metrics
  are evaluated on a later disjoint OOF segment. The deployable model is trained
  only on resolved observations before that untouched holdout cutoff.
- The training report requires resolved samples, at least 20 OOS predictions,
  ECE at most 0.15, precision above the OOS base rate and positive mean net
  return among accepted OOS predictions.
- Training writes an inactive artifact. Activation requires a separate
  operator approval call with a trusted approval_verifier supplied by the
  deployment's authenticated identity/RBAC boundary. A display name and
  rationale alone are insufficient.
- Scanner default is AITOS_FINANCIAL_ML_MODE=shadow. In enforce mode the
  meta model is an additional veto and fails closed when no approved artifact
  is loaded. It never relaxes an existing gate.

### Phase D — purged walk-forward and sequential bootstrap

- Training folds are expanding and chronological. Each training observation
  must have resolved before the first test timestamp; unresolved/overlapping
  labels are excluded and a one-observation embargo is applied.
- Sequential bootstrap weights samples by marginal uniqueness of their label
  intervals. It is applied only within each training fold, after temporal
  purging; it is not a substitute for purging.
- Calibration predictions remain out-of-fold. The final model is refit only
  after OOS reporting has completed.

### Phase E — statistical validation and multiple testing

- Financial ML OOS reporting includes Brier score, ECE, OOS precision, base
  rate and accepted-subset mean net return. A 1,000-path circular-block Monte
  Carlo stress is also run on accepted OOS returns with an additional 5 bps
  per-observation cost shock; acceptance requires loss probability no greater
  than 50% and 95th-percentile maximum drawdown no greater than 35%.
- The A-Stat selection module already provides White's Reality Check,
  Deflated/Probabilistic Sharpe, model-confidence-set diagnostics and
  multiple-hypothesis p-value correction. Apply these to the complete
  experiment/strategy search family; do not select a winner and then test only
  that winner.
- The financial_ml_validation.validate_strategy_family helper runs
  synchronized-block White's Reality Check, Benjamini-Hochberg FDR correction
  and an optional model-confidence set across aligned OOS strategy returns,
  hypothesis p-values and model losses. The learning ExperimentEngine now
  invokes it per regime/cost cohort when its evaluator supplies aligned
  net_returns (and optional hypothesis_p_value values), and retains the
  validation evidence in ExperimentReport.
- A single binary meta-model OOS report is not a substitute for strategy-family
  Reality Check/FDR analysis or a full paper-trading acceptance campaign.

### Phase F — advanced volatility diagnostics

- Scanner candidate features now include a causal AR(1) log-variance stochastic
  volatility proxy, robust-MAD jump-diffusion intensity/variance contribution,
  and a multi-scale roughness/Hurst diagnostic.
- These estimators are deterministic, bounded and use only the supplied
  historical return window. They are diagnostics/features, not full calibrated
  Heston, Bates or rough-Bergomi pricing implementations.
- Existing GJR-GARCH, EGARCH, HAR-RV, HSMM, Hawkes and DCC-GARCH/Copula
  components remain available in the wider A-Stat stack.

### Phase G — live scanner, paper lifecycle and governance

- Candidate IDs and meta evidence flow from ScanCandidate to Opportunity,
  then into Trade.agent_consensus and every lifecycle decision record.
- DecisionOutcomeLedger is migrated additively with candidate_id,
  label_resolution_time and outcome_provenance. Entry, HOLD, MANAGE, EXIT,
  execution-failure and rejection records for a candidate share that identity.
- Counterfactual barrier labels synchronize to the Decision-to-Outcome Ledger
  at the exact barrier/vertical resolution timestamp. The older 15-minute
  rejection markout reconciler skips an entity already resolved by this path.
- Actual filled trades are bound to their candidate ID; final net outcome,
  fees, funding, exit time and provenance resolve the same entity.
- The scanner exposes pipeline mode, candidate states, event-bar count and
  active approved artifact in its health snapshot.

### Operating the research pipeline

Run the manual walk-forward training command after enough labels have resolved:

    python scripts/train_financial_ml.py

The command reports OOS evidence and stores an inactive artifact; it never
approves or promotes it. The runtime defaults to shadow mode. Set
AITOS_FINANCIAL_ML_MODE=disabled to turn off feature scoring, or shadow to
observe without changing scanner eligibility. Enforce must only be enabled
after OOS acceptance, trusted operator approval, venue-cost review and a
paper canary; without an approved artifact it rejects candidates closed.

### Explicit acceptance limits

- This implementation does not claim production profitability or authorize
  live orders. It has not been empirically calibrated against exchange-specific
  historical queue position, partial-fill, market-impact and latency data.
- Counterfactual labels use the configured round-trip cost estimate; they do
  not reconstruct an actual queue/partial-fill path. Existing L2 execution
  replay remains the research route for that calibration.
- Stochastic/jump/rough volatility outputs are diagnostic estimators, not
  full likelihood-calibrated derivatives-pricing models.
- Training is deliberately manual; no background auto-training or auto-
  promotion is enabled. Trusted approval middleware must be provided before
  any production artifact activation workflow is exposed.


### Runtime storage and deployment boundary

The Financial ML candidate journal, event-bar accumulators, model artifacts and
DecisionOutcomeLedger currently use local SQLite databases. WAL mode and
transactions protect process-level durability on a single persistent local
filesystem; they do not make SQLite a distributed database.

- On Render Free, the service filesystem is ephemeral unless a supported
  persistent storage option is provisioned. A restart/redeploy can therefore
  erase pending candidates, event-bar tails and labels. Do not treat a Render
  Free ephemeral instance as a durable research journal.
- Keep this pipeline to one runtime instance on a durable local volume for
  single-instance paper testing. For horizontally scaled workers or shared
  Render deployments, migrate both journals and artifact activation to a
  transactional PostgreSQL repository before enabling enforce mode.
- No runtime database migration to PostgreSQL, Render deployment or persistent
  volume provisioning is claimed by this code change.
