# Advanced Alpha Exit Policy — staged integration

## Implemented in this branch

- A position-local adaptive exit policy supplements the existing Market State, Trade Thesis, Trade Journey and Exit Intelligence stack.
- Break-even ratchet triggers at +0.8R and proposes Entry +/- max(0.15R, estimated round-trip fees + slippage buffer), using immutable initial risk. It never loosens an existing stop.
- Stagnation guard counts position-manager evaluations and requests an exit after 35 evaluations when current progress remains below +0.4R.
- The policy is opt-in (adaptive_exit_enabled, default false) so existing paper/live behavior is unchanged until replay validation.
- Per-trade policy state is cleared with PositionManager.clear_trade.
- Unit tests cover LONG/SHORT ratchets, monotonicity, stagnation threshold and invalid input handling.

## Integration sequence

1. Entry Scanner and HTF confirmation produce a candidate.
2. Entry risk and size are validated by the existing Risk Engine.
3. Existing entry-time thesis, structural stop, path plan and immutable initial R are recorded.
4. Existing EIE evaluates thesis/structure/order-flow and retains authority for hard invalidation.
5. When explicitly enabled, adaptive policy may tighten protection or request a stagnation exit, but cannot loosen a stop or suppress an EIE EXIT.
6. Lifecycle executes the resulting action and retains existing emergency stop, partial-close and exchange reconciliation safeguards.
7. Existing excursion/journey telemetry supplies post-trade review.

## Important limitations before enabling

This branch is a safe, opt-in first integration slice, not a reproduction of the claimed 5,000-tick backtest. The repository's actual multi-regime dataset and reproducible baseline/optimized replay result were not supplied or available to this implementation. No performance improvement is claimed.

The following requested work remains gated on dedicated implementation and validation:

- ATR-derived initial stop must be calculated before sizing, with emergency-stop quantity recalculated against final initial risk. A post-entry stop adjustment is not a substitute.
- ADX 2.5R / 3.2–3.5R target expansion needs explicit target-zone, liquidity, structure and reversal vetoes in the existing Path Planner/Position Manager.
- Loss-streak sizing reduction belongs in centralized Risk Engine and must use persisted, finalized net trade outcomes, not a process-local counter.
- Stagnation currently counts PositionManager evaluations in process memory; production enablement requires persisted observation/bar identity so a restart cannot reset the timer or double-count repeated updates.
- Fee/funding/slippage estimation must be venue- and position-specific.
- Replay must include fees, funding, spread, latency, partial fills, stop gaps, exchange filters and out-of-sample/walk-forward partitions across all five regimes. Report PF, MDD, Sharpe, Calmar, expectancy, trade count and confidence intervals; compare against PR #218 merged main baseline.
- Run focused unit tests, full CI, historical replay and paper trading before enabling the feature. Do not activate live trading from unit tests alone.

## Activation

Keep disabled by default. Pass adaptive_exit_enabled: true only in isolated paper/replay configuration after persisted stagnation state and remaining validation gates above are complete.
