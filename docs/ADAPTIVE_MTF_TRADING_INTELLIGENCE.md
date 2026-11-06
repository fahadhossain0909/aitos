# Adaptive Multi-Timeframe Trading Intelligence — Implementation Notes

## Scope

This change introduces pure, deterministic decision and measurement primitives and wires the existing scanner's completed-HTF-candle path to an adaptive policy. It does not submit orders, change exchange adapters, or enable live trading.

## Implemented in this change

### P0 — Directional diagnostics
- Scanner emits a symmetric LONG/SHORT directional funnel record with structure, bias, CVD, and the selected side.
- A pure diagnostic helper reports side-specific structure, flow, and bias gates.
- Existing direction thresholds are intentionally not relaxed without replay evidence. Diagnostics identify restrictive stages before threshold changes.

### P1 — Adaptive HTF decision policy
- Added immutable timeframe/evidence/decision contracts.
- Distinguishes aligned, neutral/mixed, countertrend, conflicted, and unavailable HTF context.
- Neutral HTF can proceed only when LTF quality meets a configurable floor; countertrend requires directional structure and order-flow evidence.
- Incomplete or unavailable HTF data is never re-labelled as neutral.
- Scanner retains completed-candle HTF cache and stale-cache protection.

### P2 — LTF evidence contract
- Defines a common 0..10 LONG-oriented evidence scale; SHORT is its exact mirror.
- Scanner passes structure, order-flow and momentum into the adaptive policy.
- Current integration uses the configured primary HTF and entry timeframe. Independently fetched 1D/4H/1H/15M/5M/1M contexts, footprint/tick aggregation, and multi-cache orchestration remain deferred integration work.

### P3 — Entry/exit and risk research primitives
- Added net expectancy in R, including fees/slippage expressed as R costs.
- Added a volatility risk dampener that cannot increase base risk.
- These are measurement/policy primitives; they do not override RiskEngine, lifecycle, protective stop, TP, or execution contracts.
- Existing adaptive exit and position sizing systems remain authoritative.

### P4 — Portfolio exposure
- Added gross and signed LONG/SHORT notional aggregation for diagnostics. It does not reserve capital or replace the existing portfolio/capital gateway.

### P5 — Quant validation
- Added trade-level win rate, profit factor, expectancy and max drawdown metrics.
- Added deterministic unit tests for direction symmetry, adaptive HTF decisions, missing data, expectancy, volatility dampening, exposure and metrics.
- Historical replay, walk-forward split orchestration, leakage checks, ablation runner, and paper runtime validation are not executed by this code-only change.

## Centralized configuration

New settings in the validated tuning model and environment override whitelist:
- HTF neutral minimum LTF score (default 6.0)
- HTF countertrend minimum LTF score (default 7.0)
- HTF neutral acceptance (default true)

Existing ADX, DI-gap, HTF timeframe and lookback settings remain in force.

## Safety and rollout

- No live mode activation or automatic parameter learning is introduced.
- Missing/stale HTF data remains fail-closed.
- The policy does not promise higher win rate or profitability.
- Before enabling wider countertrend behavior, run unit tests, scanner integration tests, historical replay, walk-forward evaluation, and paper trading with LONG/SHORT-separated diagnostics.
- Compare opportunity capture, rejection outcomes, net expectancy, profit factor, drawdown, MAE/MFE, and execution costs. Do not optimize win rate alone.

## Explicitly deferred runtime work

Per the implementation request, runtime exchange/paper-trading tests are deferred for a later consolidated session. New tests are authored but are not claimed as executed or passing.
