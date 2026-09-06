# Advanced Context & AI Decision Layer

PR #116 now has an additive contextual-intelligence layer. It is deliberately
model-agnostic: deterministic market features are computed first, then the AI
Kernel receives structured evidence instead of raw, unbounded market data.

## Advanced feature set

- Volume Profile: POC, VAH, VAL, HVN/LVN and price-location/acceptance.
- Volatility Regime: ATR, percentile, compression/normal/expansion/extreme and expansion rate.
- Price Imbalance: measurable three-candle imbalance/FVG-style zones and displacement strength.
- Forced Flow Proxy: volume anomaly + displacement + CVD/positioning agreement. Exchange liquidation feeds can replace the proxy later.
- Structural Symmetry: normalized historical left/right swing analogues, scale, projected levels and symmetry-failure distance.

These are **features, not hard trading rules**. In particular, symmetry is not
assumed to predict a reversal and an imbalance is not assumed to prove
institutional order placement.

## Contextual Decision Engine

The AI layer now evaluates:

1. Market state and state-transition context.
2. Evidence reliability and contextual relevance.
3. Supporting vs opposing evidence (contradiction detection).
4. Continuation, reversal and range scenarios.
5. Target zones from symmetry/profile context.
6. Explicit invalidation conditions.
7. `no_trade` as a first-class outcome.

The engine does not call an external LLM. An LLM, ML model or RL policy can
consume its structured output later without changing the market-data or risk
contracts.

## Symmetry policy

Left/right mirroring is used only as contextual structural analogy and target
projection. A strong symmetry match can increase the continuation/reversal
scenario probability, while a large symmetry failure is recorded as contrary
evidence. It never overrides liquidity, order flow, market structure or risk
governance.

## Design principle

AITOS should grow in **information quality**, not indicator count. Existing
CVD, footprint, liquidity, Auction Market Theory, structure, VWAP, lead/lag,
OI/funding and RL signals remain primary. The advanced features add orthogonal
context rather than duplicate those signals.
