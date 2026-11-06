# Adaptive Multi-Timeframe Trading Intelligence — Implementation Notes

## Scope and safety

This branch extends AITOS decision intelligence only. It does not place orders, connect to a live exchange account, or enable live trading. Paper-trading/runtime market verification is intentionally deferred to the user's later consolidated session.

## P0 — Directional diagnostics

- Scanner emits a symmetric LONG/SHORT directional funnel record, including structure event, selected-side rejection reasons and both-side rejection attribution when no side is selected.
- Directional-gate, direction-selection, market-data sufficiency, adaptive-MTF, execution-quality, scanner terminal/error, composite-score, top-N/hysteresis ranking, capital-gate, risk-engine, governance and lifecycle-open events are persisted in a bounded SQLite ledger. Scanner timeout and exception paths are also attributed. Health diagnostics expose per-stage/per-side accepted and rejected counts plus reason attribution. Persistence is best-effort, written off the async scan loop, and cannot change the scanner's trade decision.
- Both sides expose structure pass, CVD pass, bias conflict, diagnostic acceptance, and explicit rejection reason codes.
- CHoCH/CVD thresholds remain symmetric: LONG >= 6.5 and SHORT <= 3.5. Threshold relaxation is not inferred from diagnostics.

## P1/P2 — Adaptive six-timeframe analysis

- Added a candle-close-aware multi-timeframe orchestration layer for **1D, 4H, 1H, 15M, 5M and 1M**.
- Every timeframe uses the shared `HigherTimeframeCandleCache`; cache refresh is keyed by symbol/timeframe/lookback and single-flight protected.
- Each frame reports latest closed candle timestamp, age, permitted freshness age, ADX, DI gap, structure bias, regime, cache state and candle-derived structure/CVD/momentum/liquidity/location/participation evidence.
- Consensus uses explicit timeframe role weights (1D 2.0, 4H 1.8, 1H 1.0, 15M 0.8, 5M 0.6, 1M 0.4), so macro context has greater influence than execution-timeframe noise. The weights are visible in decision diagnostics.
- The 1D/4H/1H/15M/5M/1M responsibility map is emitted with every decision. Role-specific evidence is now consumed by the MTF consensus: each timeframe's assigned evidence adjusts its directional weight, with the resulting role score and weight factor exposed in diagnostics. Location is measured against a rolling 20-candle range (premium/discount proxy); liquidity evidence uses completed-candle sweep/reclaim and wick rejection; participation compares recent versus preceding candle volume.
- Conflicting DI direction and confirmed market-structure bias is classified as neutral/mixed rather than forced into a trend label. ADX strength is normalized to the configured 0–10 evidence scale.
- Forming candles are excluded by the cache. A stale fallback, insufficient history or missing timeframe marks the stack incomplete.
- The scanner requires all six frames to be fresh and complete before accepting an MTF decision. It does not convert unavailable data into neutral evidence. Tick/order-book OFI and cancellation/replenishment evidence remain a separate downstream execution-quality gate in the scanner, not a fabricated 1M candle feature.
- All six timeframe contexts are evaluated together using a configurable strength-weighted consensus threshold. A single lower-timeframe counter-move does not automatically veto a higher-timeframe majority; balanced stacks require the LTF quality floor, and dominant countertrend stacks require strong structure + order-flow reversal evidence.
- Cache request budget, fetch concurrency and freshness grace are centrally configurable with environment overrides.
- The primary timeframe map and legacy single-HTF helper remain for compatibility; the scanner decision path now uses the six-frame analyzer.

## P3 — Expected-value entry and exit integration

- Entry authorization is enforced at the `TradeLifecycle.submit_opportunity` boundary by `CapitalGateway`, not only by scanner ranking.
- Entry EV includes calibrated loss probability, nearest TP gross return, stop loss, fee, slippage, funding, liquidity, confidence, opportunity age decay and minimum net-edge/expected-return thresholds.
- Capital authorization fails closed when EV, cost, loss probability, liquidity, age, circuit-breaker or portfolio protection gates fail.
- Exit Intelligence Engine already calculates probability-weighted path continuation value. This branch exposes its expected gain, expected loss, upside/downside probability mass and transaction-cost components in the decision feature record.
- Exit decisions continue to require structural/thesis invalidation or confirmed exit pressure; a static TP touch is not treated as a reason to abandon a still-valid runner thesis.
- Closed-trade probability calibration is persisted in SQLite and updated from the lifecycle close path using the stable trade ID as an idempotency key. A retried close/outcome event cannot increment the same observation twice; SQLite write transactions serialize competing writers, and readers refresh aggregates so separate processes observe committed outcomes.
- Late fee/funding adjustments on already-closed trades revise the original calibration label atomically rather than adding a second sample. Duplicate adjustment IDs remain idempotent.
- PositionManager now records a bounded timestamped ERE forecast at each management decision. On close, lifecycle settles each forecast against the subsequent directional price move net of the EIE estimated transaction cost; the evaluation helper exports these settled forecasts as point-in-time OOS rows. The path-based exit expectation itself remains a heuristic probability-weighted estimate, not a calibrated statistical forecast. A real OOS calibration result still requires populated closed-trade observations.

## P4 — Capital allocation and correlation-aware portfolio risk

- The lifecycle capital guard calls the shared CapitalGateway before sizing/execution.
- The gateway applies expected-edge ranking, per-trade/portfolio risk budgets, drawdown and volatility multipliers, circuit breakers, capital reservations and portfolio protection.
- Lifecycle-created gateways use a durable SQLite reservation ledger. BEGIN IMMEDIATE transactions enforce absolute equity and portfolio-risk budgets across processes, prevent duplicate same-symbol reservations by competing owners, and use bounded leases so crashed workers do not leave permanent reservations. In-memory reservations remain available for isolated tests and callers that do not configure persistence.
- Existing portfolio correlation data is consumed by PortfolioProtection. A leakage-safe point-in-time estimator now builds symmetric pairwise correlations from aligned timestamped returns at the decision cutoff, without forward filling or future observations; it is used when return history is supplied in the capital context. Unknown correlations use a conservative configured fallback.
- Adverse funding cost and leveraged-position liquidation-buffer checks are now hard risk gates. A leveraged candidate with no liquidation-buffer input fails closed; excessive adverse funding or an insufficient buffer is rejected.
- The gateway's approved risk budget is passed into the trade sizing path; the capital gate cannot be bypassed by calling lifecycle submission directly.
- Correlation estimates must be point-in-time, symmetric and refreshed from measured returns. A single portfolio-wide max-pairwise fallback is conservative but not a substitute for a complete covariance matrix.
- Durable reservation contention has both cross-instance and spawned multi-process regression coverage, including stale-budget oversubscription and lease expiry after a crashed worker. These tests still require a successful latest-head CI run.

## P5 — Historical replay, walk-forward and ablation evaluation

- Added leakage-aware chronological split generation with a configurable embargo.
- Evaluation rows require timezone-aware decision/outcome timestamps, outcome strictly after decision, finite net PnL and explicit LONG/SHORT side.
- Held-out fold summaries report trade count, expectancy, win rate, profit factor, drawdown and LONG/SHORT-separated metrics.
- Optional point-in-time observation fields now produce regime-wise scorecards, MAE/MFE, entry capture, slippage, fees and funding summaries, plus a normal-approximation expectancy interval (reported as uncertainty, not a significance/profitability claim).
- Rejected-opportunity counterfactual markouts are reported separately when their realized counterfactual net PnL is supplied; absent historical markouts are never synthesized.
- When point-in-time predicted win probabilities are present, the report adds Brier score, log loss and reliability bins against realized outcomes. Probability inputs outside [0, 1] are rejected; no forecasts are inferred from outcomes.
- Comparative reports support baseline versus ablation variants (for example removing order-flow, MTF, liquidity or footprint evidence) and calculate deltas where metrics are defined.
- Added `HistoricalAblationRunner`, which executes baseline and component-disabled strategy callables through the canonical `BacktestEngine`, then runs each variant through the existing `WalkForwardValidator` with a locked holdout.
- Added a bounded `ClickHouseReplayLoader` and `ClickHouseHistoricalAblationRunner` that load persisted trade ticks, L2 snapshots and funding rows over strict timezone-aware [start,end) ranges, preserve exchange timestamps and feed the canonical replay/walk-forward/locked-holdout pipeline.
- Sequenced depth events are now persisted separately in ClickHouse `depth_events` and attached to loaded book snapshots. The futures replay runner applies funding only when a recorded settlement timestamp is crossed, using the last already-observed mark rather than charging funding on every market event.
- The ablation report includes full replay metrics, walk-forward/holdout outcomes, LONG/SHORT-separated realized PnL metrics, unclassified-trade counts and deltas versus baseline. Strategy variants are supplied as named callables so OFI/order-flow/MTF/liquidity/footprint components can be switched off without silently changing the baseline.
- The leakage-aware observation helpers remain available for externally recorded journals; they do not synthesize historical data or claim a backtest result without the canonical replay.
- No model/strategy is promoted automatically. Use a locked holdout after walk-forward and keep paper/shadow validation as a separate approval gate.

## Tests authored

- Six-frame fetch coverage and stale-stack fail-closed behavior.
- LONG/SHORT directional diagnostics and adaptive HTF behavior.
- EV entry/cost checks already present in capital runtime tests.
- Probability-weighted exit expectancy diagnostics and mirrored SHORT path handling.
- Persistent calibration across restart, duplicate trade-outcome delivery, cross-instance deduplication, lifecycle close integration and late-settlement label correction.
- Role-specific evidence weighting in the adaptive six-timeframe consensus.
- Correlation-aware incremental portfolio risk.
- SQLite cross-instance and real multi-process capital reservation, absolute budget enforcement, duplicate-symbol contention and lease expiry.
- Sequenced LocalOrderBook → LiveScannerCache → TradeDepthMatcher → OFI aggregation replay with bid/ask execution, cancellation-proxy and replenishment attribution.
- ClickHouse replay dataset loading, persisted depth-event association and scheduled funding settlement.
- Chronological walk-forward embargo, timestamp leakage rejection, ablation reporting and LONG/SHORT comparative metrics.

## Verification status

- New regression tests now cover scanner score/ranking attribution, CHoCH-aware directional diagnostics, real multiprocess reservations, lease expiry, point-in-time correlation cutoffs, ERE forecast settlement/OOS extraction, cost sensitivity, sequenced depth-event replay and ClickHouse loader behavior.
- CI is currently running on the latest implementation head; its final result must be reviewed before the work can be called regression-verified.
- Exchange-connected runtime and paper trading remain deliberately deferred. Existing ClickHouse data has not been queried in this session, so no real-market historical replay, walk-forward metrics or locked-holdout pass/fail result is claimed.
- Historical depth-event persistence is forward-looking: snapshots recorded before the new `depth_events` table was deployed cannot be reconstructed into true diff-depth events from top-of-book snapshots alone. Backfill requires archived exchange diff-depth payloads.
- No profitability or performance improvement is claimed without historical replay on point-in-time data and a locked out-of-sample holdout.


## CI follow-up

Formatting changes were applied after the initial CI report. The prior CI attempt on commit 85da343 reported Black formatting failures in seven files; those diffs were corrected on subsequent commits. That CI run's Backtest Tests passed, but it did not verify the corrected latest head. Re-run CI on the latest branch head before treating regression verification as complete.
