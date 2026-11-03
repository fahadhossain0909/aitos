# Advanced Alpha Exit Policy — staged integration

## Implemented in this branch

- A position-local adaptive exit policy supplements the existing Market State, Trade Thesis, Trade Journey and Exit Intelligence stack.
- Break-even ratchet triggers at +0.8R and proposes Entry +/- max(0.15R, estimated round-trip fees + slippage buffer), using immutable initial risk. It never loosens an existing stop.
- Stagnation guard requests an exit after 35 position-manager observations when best recorded progress (MFE) remains below +0.4R. The observation count is stored on the Trade record and included in `to_dict()` serialization; it is not guaranteed to represent unique market bars.
- Scanner final runner target now adapts to confirmed HTF ADX when callers do not supply explicit R multiples: 2.5R for ADX 25–35 and 3.3R above 35; weak/unknown ADX retains 3R. The first two partial targets remain 1R/2R, and explicit caller settings take precedence.
- Capital Gateway halves the requested risk budget after two consecutive losses, using portfolio `consecutive_losses`; the existing five-loss hard circuit breaker remains authoritative.
- The policy is opt-in (adaptive_exit_enabled, default false) so existing paper/live behavior is unchanged until replay validation.
- The adaptive policy is opt-in (adaptive_exit_enabled, default false). Its observation counter is part of the Trade record rather than process-local policy memory.
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

This branch is a safe, opt-in integration slice, not a reproduction of the claimed 5,000-tick backtest. The repository's actual multi-regime dataset and reproducible baseline/optimized replay result were not supplied or available to this implementation. No performance improvement is claimed.

The following requested work remains gated on dedicated implementation and validation:

- ATR-derived initial stop must be calculated before sizing, with emergency-stop quantity recalculated against final initial risk. A post-entry stop adjustment is not a substitute.
- The HTF-ADX runner target is now implemented, but the final target still needs explicit target-zone/liquidity/structure/reversal vetoes and a path-aware target selector in the existing Path Planner/Position Manager. ADX-only target expansion must not be treated as a guaranteed target.
- Capital Gateway now halves the requested risk budget after two consecutive losses, using the portfolio's `consecutive_losses` field; the existing five-loss hard circuit breaker remains. The portfolio producer must populate that field from persisted, finalized net outcomes and reset it after a net-profitable close. This branch does not fabricate a new streak counter.
- Stagnation observations now serialize with the Trade record, but the persistence/recovery adapter must be verified to restore the new field. The counter increments on PositionManager evaluations, not unique candle IDs; exact bar-based semantics still require event/bar identity.
- Fee/funding/slippage estimation must be venue- and position-specific.
- Replay must include fees, funding, spread, latency, partial fills, stop gaps, exchange filters and out-of-sample/walk-forward partitions across all five regimes. Report PF, MDD, Sharpe, Calmar, expectancy, trade count and confidence intervals; compare against PR #218 merged main baseline.
- Run focused unit tests, full CI, historical replay and paper trading before enabling the feature. Do not activate live trading from unit tests alone.

## Activation

Keep disabled by default. Pass adaptive_exit_enabled: true only in isolated paper/replay configuration after persisted stagnation state and remaining validation gates above are complete.
