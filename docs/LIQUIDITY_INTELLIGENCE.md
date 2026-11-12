# AITOS Liquidity Intelligence & Stop-Hunt Defense

## Scope and safety contract

This package estimates structural liquidity pools from **completed** OHLCV candles and optional point-in-time reference levels. It classifies post-sweep behaviour as rejection, acceptance, or indecision. It does not observe hidden stop orders, prove intent, or equate every wick with a stop hunt.

The initial scanner integration is **shadow-only**. It records features for the AI Kernel context and decision attribution without changing candidate ranking, entry permission, position size, stops, or execution. Production influence requires explicit configuration, replay evidence, walk-forward/OOS validation, probability calibration, paper rollout, and champion approval.

## Existing AITOS components reused

- `aitos/intelligence/liquidity.py`: depth, OFI, cancellation/replenishment and execution-quality features.
- `aitos/intelligence/liquidity_tracker.py`: consecutive L2 stacking, pulling and sweep events.
- `aitos/intelligence/orderflow_liquidity_interaction.py`: footprint/L2 evidence fusion.
- `aitos/intelligence/structural_risk/engine.py`: structural thesis-invalidation stop selection.
- `aitos/intelligence/path_planner/`: liquidity-aware target planning and vetoes.
- `aitos/learning/`: existing outcome, experiment and walk-forward governance.

The new `liquidity_intelligence.py` complements these with candle-structure liquidity mapping, equal-high/low pools, sweep/reclaim classification, a bounded adverse-liquidity risk score, risk-budget-aware stop helpers, and outcome attribution.

## Data contract

A `LiquidityLevel` is an estimated pool, not a list of observed stop orders. Sources include equal highs/lows, confirmed swing highs/lows and explicitly supplied external references (previous day/week/session levels). External references must be point-in-time and carry their own provenance.

Pivots are only confirmed after `pivot_right` completed candles. The forming candle is excluded. A sweep is a completed candle that penetrates a pool by an ATR-normalized amount; a wick alone is insufficient for a trade signal.

## State and decision policy

- `SWEPT`: level penetrated; no reversal assumption.
- `REJECTED`: price reclaimed the level and fresh, sufficiently confident opposite-side flow confirms.
- `ACCEPTED`: consecutive completed closes hold beyond the level.
- `INDECISION`: insufficient or conflicting evidence; wait.

Sell-side sweep + bullish reclaim/flow can produce a LONG reversal candidate. Buy-side sweep + bearish reclaim/flow can produce a SHORT reversal candidate. Acceptance produces a continuation candidate, not a fade. Both directions use symmetric rules.

## Risk and execution

The stop helper places the stop beyond thesis invalidation plus a volatility/minimum buffer. If the resulting stop exceeds the configured maximum distance, it returns `allowed=False`; it never silently widens risk. Position sizing reserves estimated per-unit execution costs before calculating quantity. The existing Risk Engine, hard caps, HTF fail-closed gate, reduce-only controls, and exchange protective-stop handling remain authoritative.

## Research and rollout

1. Build point-in-time pools from historical OHLCV plus reference levels.
2. Replay sweep events with order-book/OFI/CVD freshness, trade costs, funding, latency and partial fills where available.
3. Compare baseline, mapping-only, detection-only, classifier, stop-policy and full-engine ablations.
4. Evaluate precision/recall, false-sweep rate, rejection/acceptance calibration, stop-out/reversal attribution, MAE/MFE, net expectancy, drawdown, PF, Sharpe/Sortino and adverse selection.
5. Use chronological walk-forward splits and symbol/regime OOS validation; purge overlapping labels and prohibit look-ahead.
6. Shadow -> advisory -> controlled paper only after governance approval. Never self-promote a model or change live execution policy.

## Known integration boundary

The current scanner bridge attaches the analysis to the AI Kernel context as an evidence-only field and records an attribution event. It reads trade/depth snapshots only when both timestamps are fresh (15-second maximum age), uses a normalized buy-ratio and top-of-book depth imbalance as bounded flow proxies, and includes recent bid/ask stacking evidence. Stale or missing data cannot confirm a reversal. The scanner refreshes a bounded, 30-minute-expiring structural-level snapshot in LiveMarketStateStore; MarketContextProvider forwards it to PositionManager, StructuralRiskEngine and MarketPathPlanner. This integration intentionally does not yet veto or rerank candidates. Durable ClickHouse experience persistence, true OFI/CVD event alignment and production decision gates must be completed only after replay labels and the repository's event/experience contracts are validated.
