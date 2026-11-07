# Advanced Alpha Exit Policy — staged integration

## Implemented in this branch

- A position-local adaptive exit policy supplements the existing Market State, Trade Thesis, Trade Journey and Exit Intelligence stack.
- Break-even ratchet triggers at +0.8R and proposes Entry +/- max(0.15R, estimated round-trip fees + slippage buffer), using immutable initial risk. It never loosens an existing stop.
- Dead-candle guard requests `time_decay_scratch` after 16 unique completed 15m candles (4h at the default cadence), only when recorded MFE remains below `+0.10R` and the entire observed price excursion plus current mark stayed within `±0.2%` of entry. The count and last completed-bar identity are persisted on the Trade record, so repeated ticks within one candle do not accelerate the clock.
- Scanner final runner target now adapts to confirmed HTF ADX when callers do not supply explicit R multiples: 2.5R for ADX 25–35 and 3.3R above 35; weak/unknown ADX retains 3R. The first two partial targets remain 1R/2R, and explicit caller settings take precedence.
- Capital Gateway halves the requested risk budget after two consecutive losses, using portfolio `consecutive_losses`; the existing five-loss hard circuit breaker remains authoritative.
- Adaptive break-even and dead-candle safeguards are enabled by default when EIE/PositionManager is wired. The scanner and position thresholds are centralized in `ScannerTuning` and `PositionTuning` and can be overridden through the documented `AITOS_TUNING_*` environment variables.
- The adaptive policy defaults to enabled (adaptive_exit_enabled: true). Its observation counter is part of the Trade record rather than process-local policy memory.
- Unit tests cover LONG/SHORT ratchets, monotonicity, stagnation threshold and invalid input handling.

## Integration sequence

1. Entry Scanner and HTF confirmation produce a candidate.
2. Entry risk and size are validated by the existing Risk Engine.
3. Existing entry-time thesis, structural stop, path plan and immutable initial R are recorded.
4. Existing EIE evaluates thesis/structure/order-flow and retains authority for hard invalidation.
5. When enabled (default), adaptive policy may tighten protection or request a verified dead-candle scratch exit, but cannot loosen a stop or suppress an EIE EXIT.
6. Lifecycle executes the resulting action and retains existing emergency stop, partial-close and exchange reconciliation safeguards.
7. Existing excursion/journey telemetry supplies post-trade review.

## Important limitations before enabling

This change is a deterministic risk-management guardrail, not a reproduction of the claimed 5,000-tick backtest. The repository's actual multi-regime dataset and reproducible baseline/optimized replay result were not supplied or available to this implementation. No profitability or performance improvement is claimed.

The following requested work remains gated on dedicated implementation and validation:

- ATR-derived initial stop must be calculated before sizing, with emergency-stop quantity recalculated against final initial risk. A post-entry stop adjustment is not a substitute.
- Path Planner now provides explainable path-runway checks, barrier caps, and reversal/thesis vetoes for the runner target. Remaining work is calibrating path probability/confidence thresholds and validating the model against replayed fills; ADX-only target expansion must not be treated as a guaranteed target.
- Capital Gateway now halves the requested risk budget after two consecutive losses, using the portfolio's `consecutive_losses` field; the existing five-loss hard circuit breaker remains. The portfolio producer must populate that field from persisted, finalized net outcomes and reset it after a net-profitable close. This branch does not fabricate a new streak counter.
- Completed-bar stagnation state (`adaptive_exit_observations` and `adaptive_exit_last_bar_id`) is serialized and restored with the Trade record. Runtime acceptance still requires multi-process/restart validation against the deployed market-data clock and candle-close identity.
- Fee/funding/slippage estimation must be venue- and position-specific.
- Replay must include fees, funding, spread, latency, partial fills, stop gaps, exchange filters and out-of-sample/walk-forward partitions across all five regimes. Report PF, MDD, Sharpe, Calmar, expectancy, trade count and confidence intervals; compare against PR #218 merged main baseline.
- Run focused unit tests, full CI, historical replay and paper trading before enabling the feature. Do not activate live trading from unit tests alone.

## Activation

Adaptive exit is enabled by default. Set adaptive_exit_enabled: false to roll back. The default guardrails now apply wherever EIE/PositionManager is enabled. The paper-only six-hour backstop additionally requires a valid market mark received within the prior five minutes and persisted Trade excursion telemetry; stale or missing mark timestamps fail closed. The stricter live deployment should remain behind the project's paper/replay acceptance gate: verify unique-bar recovery, mark freshness, fees/slippage, exchange close confirmation and out-of-sample impact before enabling or relying on them in live execution.
